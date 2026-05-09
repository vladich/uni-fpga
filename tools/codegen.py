"""
Generate `top.sv` for a resolved configuration.

The generated module:
  - declares the FPGA's external pins as top-level ports (one port per pin or
    pin-bank that any attached peripheral binds to);
  - establishes context wires (clk, rst, rst_n) from the clock/reset providers;
  - declares one bus per capability, sized by the sum of provider widths;
  - for each attached peripheral, either wires it through directly (passthrough)
    or instantiates its driver SV module with port_map + pin_assigns;
  - instantiates `design_top` at the bottom with parameter values and capability
    buses wired to its ports.

Phases:
  1. Index — load capabilities, compute per-capability widths and offsets.
  2. Plan FPGA ports — figure out which board pin banks are used by any
     peripheral's bind and expose them as module ports.
  3. Emit SV — write the top module text using the plan.

The output is a single string. Callers (synthesize.py) write it to disk.
"""

import logging
import os
import re
import sys
from collections import OrderedDict, defaultdict

import yaml

from config import init as config_init


log = logging.getLogger(__name__)
REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


# ---------------------------------------------------------------------------
# Helpers for parsing references in YAML port_maps and configuration binds
# ---------------------------------------------------------------------------

# A configuration's bind: RHS like `onboard_switches`, `pmod_jc[2]`,
# `onboard_7seg.anodes`, `onboard_7seg.anodes[0]`.
_BANK_REF = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)"        # bank name
    r"(?:\.([A-Za-z_][A-Za-z0-9_]*))?"     # optional .subkey
    r"(?:\[(\d+)\])?"                      # optional [index]
    r"\s*$"
)


def _parse_bank_ref(text):
    """`pmod_jc[2]` -> ('pmod_jc', None, 2). `onboard_7seg.dp` -> ('onboard_7seg', 'dp', None)."""
    m = _BANK_REF.match(text)
    if not m:
        return None
    bank, sub, idx = m.group(1), m.group(2), m.group(3)
    return (bank, sub, int(idx) if idx is not None else None)


def _bank_pin(pinmap, bank_name, subkey, index):
    """Look up a single pin (or list of pins) inside a board's pinBanks."""
    bank = pinmap.get("pinBanks", {}).get(bank_name)
    if bank is None:
        return None
    pins = bank.get("pins") if isinstance(bank, dict) else None
    if pins is None:
        return None
    if subkey is not None:
        sub = pins.get(subkey) if isinstance(pins, dict) else None
        if sub is None:
            return None
        if index is None:
            return sub
        return sub[index] if isinstance(sub, list) and index < len(sub) else None
    if index is not None:
        return pins[index] if isinstance(pins, list) and index < len(pins) else None
    return pins


def _bank_width(pinmap, bank_name, subkey=None):
    """Number of pins the given bank (or subkey) holds. None if not a list."""
    bank = pinmap.get("pinBanks", {}).get(bank_name) or {}
    pins = bank.get("pins")
    if subkey is not None and isinstance(pins, dict):
        pins = pins.get(subkey)
    if isinstance(pins, list):
        return len(pins)
    if isinstance(pins, dict):
        return None      # nested
    return 1             # scalar


# ---------------------------------------------------------------------------
# Phase 1: index capabilities
# ---------------------------------------------------------------------------

class CapabilityPlan:
    """Per-capability aggregation plan: total width and per-provider offset/width."""

    def __init__(self, cap_id, cap_def):
        self.id = cap_id
        self.cap = cap_def
        self.aggregation = cap_def.get("aggregation")
        self.providers = []         # list of (peripheral_idx, params)
        self.offsets = {}           # peripheral_idx -> bit offset (concat only)
        self.widths = {}            # peripheral_idx -> width
        self.params = {}            # final resolved capability params (e.g. width, screen_width)

    def add_provider(self, peripheral_idx, peripheral_def, params):
        self.providers.append((peripheral_idx, peripheral_def, params))


def _eval_param(spec, peripheral_params, peripheral_def=None):
    """Resolve a peripheral param spec — either a literal or `$<name>`
    referring to a peripheral-instance parameter (or its default from the
    peripheral YAML when the configuration doesn't override it)."""
    if isinstance(spec, str) and spec.startswith("$"):
        key = spec[1:]
        v = peripheral_params.get(key)
        if v is not None:
            return v
        if peripheral_def is not None:
            param_def = (peripheral_def.get("parameters") or {}).get(key) or {}
            return param_def.get("default")
        return None
    return spec


_PRIMARY_PARAM = {
    "switches":      "width",
    "buttons":       "width",
    "leds":          "width",
    "rgb_leds":      "count",
    "seven_segment": "digits",
    "gpio":          "width",
}


def build_capability_plans(resolved):
    capabilities = config_init.read_capabilities()
    plans = OrderedDict((cid, CapabilityPlan(cid, cdef)) for cid, cdef in capabilities.items())

    for idx, attach in enumerate(resolved["peripherals"]):
        perif = attach["peripheral"]
        params = attach.get("params") or {}
        for entry in perif.get("provides") or []:
            cap_id = entry["capability"]
            plan = plans[cap_id]
            cap_params_spec = entry.get("params") or {}
            cap_params_resolved = {k: _eval_param(v, params, perif) for k, v in cap_params_spec.items()}
            plan.add_provider(idx, perif, cap_params_resolved)

    # Compute widths and offsets per aggregation rule.
    for plan in plans.values():
        if not plan.providers:
            continue
        if plan.aggregation == "exclusive":
            if len(plan.providers) > 1:
                ids = ", ".join(p[1]["id"] for p in plan.providers)
                log.warning(
                    "Capability '%s' is exclusive but %d peripherals provide it (%s) — "
                    "using the first provider; remove duplicates from the configuration "
                    "to silence this warning.",
                    plan.id, len(plan.providers), ids,
                )
                plan.providers = plan.providers[:1]
            _, _, params = plan.providers[0]
            plan.params = dict(params)
        elif plan.aggregation == "concat":
            primary = _PRIMARY_PARAM.get(plan.id, "width")
            offset = 0
            for pidx, perif, params in plan.providers:
                w = params.get(primary) or params.get("width") or params.get("count") \
                        or params.get("digits") or 1
                plan.offsets[pidx] = offset
                plan.widths[pidx] = w
                offset += w
            plan.params = {primary: offset}
        else:
            plan.params = {}

    return plans


# ---------------------------------------------------------------------------
# Phase 2: plan FPGA top-level ports
# ---------------------------------------------------------------------------

def collect_referenced_banks(resolved):
    """Return ordered set of bank names referenced by any peripheral binding."""
    banks = OrderedDict()
    for attach in resolved["peripherals"]:
        for ref in (attach.get("bind") or {}).values():
            for one in (ref if isinstance(ref, list) else [ref]):
                parsed = _parse_bank_ref(one) if isinstance(one, str) else None
                if parsed is None:
                    continue
                banks[parsed[0]] = True
    return list(banks.keys())


def fpga_port_decls(resolved, referenced_banks):
    """Emit the FPGA top module's port list. Direction is inferred per sub-key
    when a bank has differently-directed pins (e.g. UART tx/rx)."""
    pinmap = resolved["board_pinmap"]
    decls = []
    ports = []

    for bank_name in referenced_banks:
        bank = (pinmap.get("pinBanks") or {}).get(bank_name) or {}
        pins = bank.get("pins")

        if isinstance(pins, str) or pins is None:
            d = _infer_pin_direction(resolved, bank_name, None)
            ports.append((bank_name, 1, d))
            decls.append("    {dir:<6s} {name}".format(dir=_dir_kw(d), name=bank_name))
        elif isinstance(pins, list):
            w = len(pins)
            d = _infer_pin_direction(resolved, bank_name, None)
            ports.append((bank_name, w, d))
            decls.append("    {dir:<6s} [{hi}:0] {name}"
                         .format(dir=_dir_kw(d), hi=w-1, name=bank_name))
        elif isinstance(pins, dict):
            for sub, val in pins.items():
                pname = "{}_{}".format(bank_name, sub)
                d = _infer_pin_direction(resolved, bank_name, sub)
                if isinstance(val, list):
                    w = len(val)
                    ports.append((pname, w, d))
                    decls.append("    {dir:<6s} [{hi}:0] {name}"
                                 .format(dir=_dir_kw(d), hi=w-1, name=pname))
                else:
                    ports.append((pname, 1, d))
                    decls.append("    {dir:<6s} {name}"
                                 .format(dir=_dir_kw(d), name=pname))
    return ",\n".join(decls), ports


def _infer_pin_direction(resolved, bank_name, subkey):
    """Determine direction for a specific bank pin (or sub-keyed pin set)."""
    has_in = has_out = has_inout = False
    for attach in resolved["peripherals"]:
        perif = attach["peripheral"]
        sig_dirs = {s["name"]: s.get("direction", "inout") for s in perif.get("signals", [])}
        for sig_name, ref in (attach.get("bind") or {}).items():
            for one in (ref if isinstance(ref, list) else [ref]):
                if not isinstance(one, str):
                    continue
                parsed = _parse_bank_ref(one)
                if parsed is None or parsed[0] != bank_name:
                    continue
                # Only consider this binding if its sub-key matches what we're inferring.
                if subkey is not None and parsed[1] != subkey:
                    continue
                if subkey is None and parsed[1] is not None:
                    continue
                d = sig_dirs.get(sig_name)
                if d == "input":
                    has_in = True
                elif d == "output":
                    has_out = True
                else:
                    has_inout = True
    if has_inout or (has_in and has_out):
        return "inout"
    if has_in:
        return "input"
    if has_out:
        return "output"
    return "inout"


def _dir_kw(direction):
    return {"input": "input", "output": "output", "inout": "inout"}[direction]




# ---------------------------------------------------------------------------
# Phase 3: emit SV
# ---------------------------------------------------------------------------

def emit_top_sv(resolved):
    cfg = resolved["configuration"]
    board = resolved["board"]
    pinmap = resolved["board_pinmap"]
    toolchain = resolved["toolchain"]

    plans = build_capability_plans(resolved)
    referenced_banks = collect_referenced_banks(resolved)

    out = []
    out.append("// =============================================================================")
    out.append("// Auto-generated top.sv — DO NOT EDIT")
    out.append("// Configuration: {}".format(cfg["id"]))
    out.append("// Board:         {} ({})".format(board.get("BoardName", board["Id"]), board["Id"]))
    out.append("// Toolchain:     {}".format(toolchain["Id"]))
    out.append("// Generated by tools/codegen.py from config/configurations/{}.yml".format(cfg["id"]))
    out.append("// =============================================================================")
    out.append("")

    # ---- FPGA module header ----
    port_text, _ports = fpga_port_decls(resolved, referenced_banks)
    out.append("module top (")
    out.append(port_text)
    out.append(");")
    out.append("")

    # ---- Context wires ----
    out.extend(_emit_context(resolved, plans))
    out.append("")

    # ---- Capability bus declarations ----
    out.extend(_emit_capability_busses(plans))
    out.append("")

    # ---- Peripheral wiring (passthroughs + driver instances) ----
    for idx, attach in enumerate(resolved["peripherals"]):
        out.append("    // ---- {} (peripheral '{}') ----"
                   .format(_attach_label(attach), attach["peripheral_id"]))
        out.extend(_emit_attachment(resolved, idx, attach, plans))
        out.append("")

    # ---- design_top instantiation ----
    out.extend(_emit_lab_top(resolved, plans))
    out.append("")
    out.append("endmodule")
    return "\n".join(out)


def _attach_label(attach):
    bind = attach.get("bind") or {}
    if not bind:
        return attach["peripheral_id"]
    sample = next(iter(bind.values()))
    if isinstance(sample, list):
        sample = sample[0] if sample else "?"
    return "{} on {}".format(attach["peripheral_id"], sample)


# ---- Context emission ---------------------------------------------------

def _emit_context(resolved, plans):
    lines = []
    lines.append("    // ---- Context: clk, rst, rst_n ----")

    clk_plan = plans["clock"]
    rst_plan = plans["reset"]

    if clk_plan.providers:
        clk_attach = resolved["peripherals"][clk_plan.providers[0][0]]
        clk_bank = (clk_attach.get("bind") or {}).get("clk")
        if clk_bank is None:
            # Heuristic fallback: use the first FPGA pin bank whose name starts with `clk`.
            for bank_name in (resolved["board_pinmap"].get("pinBanks") or {}):
                if bank_name.lower().startswith("clk") or "clock" in bank_name.lower():
                    clk_bank = bank_name
                    break
        if clk_bank is None:
            log.warning("Configuration %s: no clock bound; using literal clk port",
                        resolved["configuration"]["id"])
            clk_bank = "clk"
        clk_port = _bank_ref_to_port(clk_bank)
        if clk_port == "clk":
            # FPGA top-level port is already named `clk`; emit nothing — the
            # port is directly visible as the system clock wire.
            lines.append("    // System clock comes from the top-level `clk` port directly.")
        else:
            lines.append("    wire clk = {};".format(clk_port))
    else:
        log.warning("Configuration %s: no clock provider — generated top will not work as-is",
                    resolved["configuration"]["id"])
        lines.append("    wire clk = 1'b0;   // TODO: no clock provider")

    # Reset: OR all reset providers; invert if active=low.
    if rst_plan.providers:
        terms = []
        for pidx, perif, _ in rst_plan.providers:
            r_attach = resolved["peripherals"][pidx]
            rst_bank = (r_attach.get("bind") or {}).get("rst")
            if rst_bank is None:
                continue
            active = (r_attach.get("params") or {}).get("active") or "low"
            ref = _bank_ref_to_port(rst_bank) if isinstance(rst_bank, str) else rst_bank
            terms.append("(~ {})".format(ref) if active == "low" else "({})".format(ref))
        lines.append("    wire rst   = {};".format(" | ".join(terms) if terms else "1'b0"))
    else:
        lines.append("    wire rst   = 1'b0;   // no reset provider")
    lines.append("    wire rst_n = ~ rst;")

    # Advertise clk_mhz; default 50 if no provider supplies a frequency.
    freq = None
    if clk_plan.providers:
        freq = clk_plan.params.get("frequency_mhz") or (clk_plan.providers[0][2] or {}).get("frequency_mhz")
    if freq is None:
        freq = 50
    lines.append("    localparam int clk_mhz = {};".format(int(freq)))
    return lines


# ---- Capability bus declarations -----------------------------------------

def _emit_capability_busses(plans):
    lines = ["    // ---- Capability buses ----"]
    for cap_id, plan in plans.items():
        if not plan.providers:
            continue
        for sig in plan.cap.get("signals", []):
            sig_name = sig["name"]
            if sig.get("type") == "scalar":
                lines.append("    wire cap_{}_{};".format(cap_id, sig_name))
            else:
                width = _signal_width(plan, sig)
                lines.append("    wire [{}:0] cap_{}_{};".format(width-1, cap_id, sig_name))
    return lines


def _signal_width(plan, sig):
    """Resolve a capability signal's bit width using the plan's params."""
    raw = sig.get("width")
    if raw is None:
        # Default for 'bus' signals (e.g. screen.x/y) — derive from params.
        if plan.id == "screen" and sig["name"] == "x":
            from math import ceil, log2
            w = plan.params.get("width", 1) or 1
            return max(1, int(ceil(log2(max(2, w)))))
        if plan.id == "screen" and sig["name"] == "y":
            from math import ceil, log2
            h = plan.params.get("height", 1) or 1
            return max(1, int(ceil(log2(max(2, h)))))
        if plan.id == "screen" and sig["name"] in ("red", "green", "blue"):
            depth = plan.params.get("color_depth", 444)
            return _channel_width(depth, sig["name"])
        return 1
    if isinstance(raw, str) and raw.startswith("$"):
        key = raw[1:]
        v = plan.params.get(key)
        if v is None:
            return 1
        return int(v)
    return int(raw)


def _channel_width(depth, channel):
    if depth == 444:
        return 4
    if depth == 565:
        return {"red": 5, "green": 6, "blue": 5}[channel]
    if depth == 888:
        return 8
    return 4


# ---- Attachment emission --------------------------------------------------

def _emit_attachment(resolved, idx, attach, plans):
    perif = attach["peripheral"]
    if perif.get("driver") is None:
        return _emit_passthrough(resolved, idx, attach, plans)
    return _emit_driver_instance(resolved, idx, attach, plans)


def _peripheral_active_polarity(perif, attach):
    """Returns 'high' or 'low'. Configuration `params:` overrides the
    peripheral YAML's `parameters.active.default`."""
    cfg_params = attach.get("params") or {}
    if "active" in cfg_params:
        return cfg_params["active"]
    pdef = (perif.get("parameters") or {}).get("active") or {}
    return pdef.get("default") or "high"


def _emit_passthrough(resolved, idx, attach, plans):
    """For peripherals with driver: null. Wire pin signals to capability slices
    or vice versa, depending on each capability's aggregation rule and the
    peripheral signal's direction. Inverts when the peripheral is active-low."""
    lines = []
    perif = attach["peripheral"]
    bind = attach.get("bind") or {}
    active = _peripheral_active_polarity(perif, attach)
    inv = "~ " if active == "low" else ""

    for entry in perif.get("provides") or []:
        cap_id = entry["capability"]
        plan = plans[cap_id]
        if plan.aggregation == "exclusive":
            for cap_sig in plan.cap.get("signals", []):
                cap_sig_name = cap_sig["name"]
                pin_sig_name = cap_sig_name
                if pin_sig_name not in bind:
                    continue
                pin_expr = _resolve_ref("pin." + pin_sig_name, attach, plans, bind)
                cap_target = "cap_{}_{}".format(cap_id, cap_sig_name)
                if cap_sig.get("direction") == "user_to_hw":
                    lines.append("    assign {} = {}{};".format(pin_expr, inv, cap_target))
                else:
                    lines.append("    assign {} = {}{};".format(cap_target, inv, pin_expr))
        elif plan.aggregation == "concat":
            offset = plan.offsets[idx]
            width = plan.widths[idx]
            for cap_sig in plan.cap.get("signals", []):
                cap_sig_name = cap_sig["name"]
                pin_sig_name = cap_sig_name
                if pin_sig_name not in bind:
                    continue
                pin_expr = _resolve_ref("pin." + pin_sig_name, attach, plans, bind)
                if width == 1:
                    slice_expr = "cap_{}_{}[{}]".format(cap_id, cap_sig_name, offset)
                else:
                    slice_expr = "cap_{}_{}[{}:{}]".format(cap_id, cap_sig_name,
                                                           offset+width-1, offset)
                if cap_sig.get("direction") == "user_to_hw":
                    lines.append("    assign {} = {}{};".format(pin_expr, inv, slice_expr))
                else:
                    lines.append("    assign {} = {}{};".format(slice_expr, inv, pin_expr))
        elif plan.aggregation == "or":
            pass

    # Apply pin_assigns from the peripheral YAML. For active-low peripherals,
    # invert the RHS when both sides aren't already inverted.
    for lhs, rhs in (perif.get("pin_assigns") or {}).items():
        lhs_resolved = _resolve_ref(lhs, attach, plans, bind, lhs_context=True, slice_for_idx=idx)
        rhs_resolved = _resolve_ref(rhs, attach, plans, bind, slice_for_idx=idx)
        # Only auto-invert when RHS comes from a capability (active-high
        # user-perspective signal heading to an active-low pin).
        wants_invert = (active == "low"
                        and isinstance(rhs, str)
                        and rhs.lstrip("~ ").strip().startswith("capability."))
        if wants_invert:
            rhs_resolved = "~ ({})".format(rhs_resolved)
        lines.append("    assign {} = {};".format(lhs_resolved, rhs_resolved))

    if not lines:
        lines.append("    // (passthrough — no provided capabilities or no matching signals)")
    return lines


def _emit_driver_instance(resolved, idx, attach, plans):
    lines = []
    perif = attach["peripheral"]
    drv = perif["driver"]
    bind = attach.get("bind") or {}
    inst_name = "i_{}_{}".format(perif["id"], idx)

    # Driver parameters
    param_decls = []
    for pname, pval in (drv.get("parameters") or {}).items():
        param_decls.append(".{}({})".format(pname, _resolve_ref(pval, attach, plans, bind)))

    lines.append("    {mod} {params}{inst} (".format(
        mod=drv["module"],
        params=("# (" + ", ".join(param_decls) + ") ") if param_decls else "",
        inst=inst_name))

    # Driver port_map. `slice_for_idx` ensures that capability refs are
    # narrowed to THIS peripheral's slice when the capability is concat with
    # multiple providers (e.g. tm1638's `keys` port wires to its 8 of the
    # combined switches bus, not the full bus).
    port_lines = []
    for port, ref in (drv.get("port_map") or {}).items():
        if ref is None or ref == "":
            port_lines.append("        .{}()".format(port))
        else:
            port_lines.append("        .{}({})".format(
                port, _resolve_ref(ref, attach, plans, bind, slice_for_idx=idx)))
    lines.append(",\n".join(port_lines))
    lines.append("    );")

    # pin_assigns (combinational connections outside the driver instance)
    for lhs, rhs in (perif.get("pin_assigns") or {}).items():
        lhs_resolved = _resolve_ref(lhs, attach, plans, bind, lhs_context=True, slice_for_idx=idx)
        rhs_resolved = _resolve_ref(rhs, attach, plans, bind, slice_for_idx=idx)
        lines.append("    assign {} = {};".format(lhs_resolved, rhs_resolved))
    return lines


def _bank_ref_to_port(ref):
    """`onboard_rgb_led_0.r`  -> `onboard_rgb_led_0_r`.
       `pmod_jc[2]`           -> `pmod_jc[2]`.
       `onboard_7seg.anodes[0]` -> `onboard_7seg_anodes[0]`."""
    if not isinstance(ref, str):
        return ref
    s = ref.strip()
    # Split index suffix off, translate dots, re-attach.
    m = re.match(r"^(.+?)(\[\d+\])$", s)
    suffix = ""
    if m:
        s, suffix = m.group(1), m.group(2)
    return s.replace(".", "_") + suffix


def _resolve_ref(ref, attach, plans, bind, lhs_context=False, slice_for_idx=None):
    """Translate a YAML reference (`pin.x`, `capability.<id>.<sig>`, `context.<x>`,
    `const.<v>`) into the SV expression usable in the generated top module.

    `slice_for_idx`: when set to a peripheral index, capability refs to a
    concat-aggregated bus get sliced to THIS peripheral's contribution
    (`cap_<id>_<sig>[off+w-1:off]`). Without this, multi-provider concat
    capabilities would silently drive overlapping slices."""
    if ref is None:
        return ""
    if isinstance(ref, list):
        return "{" + ", ".join(_resolve_ref(x, attach, plans, bind, lhs_context, slice_for_idx) for x in ref) + "}"
    if not isinstance(ref, str):
        return str(ref)
    s = ref.strip()
    invert = ""
    if s.startswith("~"):
        invert = "~"
        s = s[1:].strip()

    # Optional indexing/slicing suffix:  `pin.d_p[0]`, `cap.x.y[7:0]`.
    idx_suffix = ""
    m = re.match(r"^(.+?)(\[\d+(?::\d+)?\])$", s)
    if m:
        s, idx_suffix = m.group(1), m.group(2)

    if s.startswith("pin."):
        pin_sig = s[len("pin."):]
        bound = bind.get(pin_sig)
        if bound is None:
            return invert + pin_sig + idx_suffix
        if isinstance(bound, list):
            # Multi-pin peripheral signal: emit a concat of the bank pins.
            # Convention: list[0] is bit 0 (LSB-first in YAML).
            inner = ", ".join(_bank_ref_to_port(b) for b in reversed(bound))
            return invert + "{" + inner + "}" + idx_suffix
        return invert + _bank_ref_to_port(bound) + idx_suffix
    if s.startswith("capability."):
        rest = s[len("capability."):]
        parts = rest.split(".")
        if len(parts) >= 2:
            cap_id = parts[0]
            sig = "_".join(parts[1:])
            base = "cap_{}_{}".format(cap_id, sig)
            # Apply per-peripheral slice when requested AND this capability
            # is concat-aggregated with multiple providers.
            if slice_for_idx is not None and not idx_suffix:
                plan = plans.get(cap_id)
                if (plan is not None and plan.aggregation == "concat"
                        and len(plan.providers) > 1
                        and slice_for_idx in plan.offsets):
                    off = plan.offsets[slice_for_idx]
                    w = plan.widths[slice_for_idx]
                    idx_suffix = "[{}]".format(off) if w == 1 else "[{}:{}]".format(off+w-1, off)
            return invert + base + idx_suffix
    if s.startswith("context."):
        return invert + s[len("context."):] + idx_suffix
    if s.startswith("const."):
        v = s[len("const."):]
        return invert + ("1'b" + v if v in ("0", "1") else v) + idx_suffix
    return invert + s + idx_suffix


# ---- design_top instantiation -----------------------------------------------

def _emit_lab_top(resolved, plans):
    lines = ["    // ---- User logic (design_top) ----"]

    cap_widths = {
        "switches":      plans["switches"].params.get("width", 0)      if plans["switches"].providers else 0,
        "buttons":       plans["buttons"].params.get("width", 0)       if plans["buttons"].providers else 0,
        "leds":          plans["leds"].params.get("width", 0)          if plans["leds"].providers else 0,
        "rgb_leds":      plans["rgb_leds"].params.get("width", 0)      if plans["rgb_leds"].providers else 0,
        "seven_segment": plans["seven_segment"].params.get("digits", 0) if plans["seven_segment"].providers else 0,
        "gpio":          plans["gpio"].params.get("width", 0)          if plans["gpio"].providers else 0,
    }

    if plans["screen"].providers:
        sp = plans["screen"].params
        sw, sh = sp.get("width", 0), sp.get("height", 0)
        depth = sp.get("color_depth", 444)
        wr = _channel_width(depth, "red")
        wg = _channel_width(depth, "green")
        wb = _channel_width(depth, "blue")
    else:
        sw = sh = wr = wg = wb = 0

    clk_mhz = (plans["clock"].providers[0][2] or {}).get("frequency_mhz") if plans["clock"].providers else None
    if clk_mhz is None:
        # Fallback: try to parse the clock bank name (e.g. "clk100mhz" -> 100).
        if plans["clock"].providers:
            clk_attach = resolved["peripherals"][plans["clock"].providers[0][0]]
            clk_bank = (clk_attach.get("bind") or {}).get("clk", "")
            m = re.search(r"(\d+)mhz", str(clk_bank).lower())
            if m:
                clk_mhz = int(m.group(1))
        if clk_mhz is None:
            clk_mhz = 50

    params = [
        ("clk_mhz",       int(clk_mhz)),
        ("w_sw",          cap_widths["switches"]),
        ("w_btn",         cap_widths["buttons"]),
        ("w_led",         cap_widths["leds"]),
        ("w_digit",       cap_widths["seven_segment"]),
        ("w_rgb_led",     cap_widths["rgb_leds"]),
        ("screen_width",  sw),
        ("screen_height", sh),
        ("w_red",         wr),
        ("w_green",       wg),
        ("w_blue",        wb),
        ("w_gpio",        cap_widths["gpio"]),
    ]
    param_block = ",\n".join("        .{}({})".format(n, v) for n, v in params)
    lines.append("    design_top # (")
    lines.append(param_block)
    lines.append("    ) i_design_top (")

    port_lines = [
        "        .clk(clk)",
        "        .rst(rst)",
        "        .sw(cap_switches_sw)"        if plans["switches"].providers      else "        .sw('0)",
        "        .btn(cap_buttons_btn)"       if plans["buttons"].providers       else "        .btn('0)",
        "        .led(cap_leds_led)"          if plans["leds"].providers          else "        .led()",
        "        .abcdefgh(cap_seven_segment_abcdefgh)" if plans["seven_segment"].providers else "        .abcdefgh()",
        "        .digit(cap_seven_segment_digit)"       if plans["seven_segment"].providers else "        .digit()",
        "        .rgb_r(cap_rgb_leds_r)"      if plans["rgb_leds"].providers      else "        .rgb_r()",
        "        .rgb_g(cap_rgb_leds_g)"      if plans["rgb_leds"].providers      else "        .rgb_g()",
        "        .rgb_b(cap_rgb_leds_b)"      if plans["rgb_leds"].providers      else "        .rgb_b()",
        "        .x(cap_screen_x)"            if plans["screen"].providers        else "        .x('0)",
        "        .y(cap_screen_y)"            if plans["screen"].providers        else "        .y('0)",
        "        .red(cap_screen_red)"        if plans["screen"].providers        else "        .red()",
        "        .green(cap_screen_green)"    if plans["screen"].providers        else "        .green()",
        "        .blue(cap_screen_blue)"      if plans["screen"].providers        else "        .blue()",
        "        .mic_sample(cap_audio_in_sample)" if plans["audio_in"].providers else "        .mic_sample('0)",
        "        .mic_valid(cap_audio_in_valid)"   if plans["audio_in"].providers else "        .mic_valid(1'b0)",
        "        .sound(cap_audio_out_sample)"     if plans["audio_out"].providers else "        .sound()",
        "        .uart_rx(cap_serial_console_rx)"  if plans["serial_console"].providers else "        .uart_rx(1'b1)",
        "        .uart_tx(cap_serial_console_tx)"  if plans["serial_console"].providers else "        .uart_tx()",
        "        .gpio(cap_gpio_io)"               if plans["gpio"].providers     else "        .gpio()",
    ]
    lines.append(",\n".join(port_lines))
    lines.append("    );")
    return lines


# ---------------------------------------------------------------------------
# XDC constraint emission (Vivado / Xilinx)
# ---------------------------------------------------------------------------

def emit_xdc(resolved):
    """Emit a Vivado XDC constraint file mapping every top-level FPGA port to
    its physical PACKAGE_PIN + IOSTANDARD. Includes clock create_clock entries
    for any clock-providing peripheral with a known frequency."""
    cfg = resolved["configuration"]
    pinmap = resolved["board_pinmap"]
    default_iostd = (pinmap.get("defaults") or {}).get("iostandard") or "LVCMOS33"

    out = []
    out.append("# =============================================================================")
    out.append("# Auto-generated XDC constraints — DO NOT EDIT")
    out.append("# Configuration: {}".format(cfg["id"]))
    out.append("# Board:         {}".format(resolved["board"].get("BoardName", resolved["board"]["Id"])))
    out.append("# =============================================================================")
    out.append("")
    # Silence Vivado's CFGBVS DRC warning. 3.3 V is the universal default for
    # 7-series education boards; configurations needing 1.8 V can override this
    # constraint downstream.
    out.append("set_property CFGBVS VCCO       [current_design];")
    out.append("set_property CONFIG_VOLTAGE 3.3 [current_design];")
    out.append("")

    referenced = collect_referenced_banks(resolved)
    plans = build_capability_plans(resolved)

    # ---- Pin assignments per bank/sub-key/index ----
    for bank_name in referenced:
        bank = (pinmap.get("pinBanks") or {}).get(bank_name)
        if bank is None:
            out.append("# WARNING: bank '{}' referenced but not in pinBanks".format(bank_name))
            continue
        pins = bank.get("pins")
        overrides = (bank.get("overrides") or {})
        bank_iostd = bank.get("iostandard") or default_iostd

        if isinstance(pins, str):
            out.append(_xdc_line(pins, bank_name, _pin_iostd(pins, overrides, bank_iostd)))
        elif isinstance(pins, list):
            for i, p in enumerate(pins):
                if p is None:
                    continue
                port = "{}[{}]".format(bank_name, i)
                out.append(_xdc_line(p, port, _pin_iostd(p, overrides, bank_iostd)))
        elif isinstance(pins, dict):
            for sub, val in pins.items():
                pname = "{}_{}".format(bank_name, sub)
                if isinstance(val, list):
                    for i, p in enumerate(val):
                        if p is None:
                            continue
                        port = "{}[{}]".format(pname, i)
                        out.append(_xdc_line(p, port, _pin_iostd(p, overrides, bank_iostd)))
                elif isinstance(val, str):
                    out.append(_xdc_line(val, pname, _pin_iostd(val, overrides, bank_iostd)))

    # ---- Clock create_clock entries ----
    out.append("")
    out.append("# ---- Clock definitions ----")
    for pidx, perif, params in plans["clock"].providers:
        attach = resolved["peripherals"][pidx]
        clk_bank = (attach.get("bind") or {}).get("clk")
        if clk_bank is None:
            continue
        port = _bank_port_name(clk_bank)
        freq = (params or {}).get("frequency_mhz")
        if freq is None:
            m = re.search(r"(\d+)mhz", str(clk_bank).lower())
            if m:
                freq = int(m.group(1))
        if freq is None:
            continue
        period_ns = 1000.0 / float(freq)
        out.append(
            "create_clock -name sys_clk_{f}mhz -period {p:.3f} -waveform {{0 {h:.3f}}} "
            "[get_ports {{ {port} }}];".format(f=int(freq), p=period_ns, h=period_ns/2.0, port=port)
        )

    out.append("")
    return "\n".join(out)


def _xdc_line(pin, port_expr, iostd):
    pin_str = str(pin)
    # Quote pin names that contain commas (Gowin diff pairs) — Vivado doesn't
    # use those, but the harvested data may still carry them; warn.
    if "," in pin_str:
        return ("# WARNING: pin '{p}' for port '{port}' is a differential pair; "
                "Vivado XDC needs the pair handled in the SV (LVDS / OBUFDS).".format(
                    p=pin_str, port=port_expr))
    return (
        "set_property -dict {{ PACKAGE_PIN {pin} IOSTANDARD {std} }} "
        "[get_ports {{ {port} }}];".format(pin=pin_str, std=iostd, port=port_expr)
    )


def emit_xdc_simple(resolved):
    """Like emit_xdc(), but emits the simple 4-arg `set_property NAME VAL
    [get_ports …]` form that nextpnr-xilinx (openxc7) accepts. Vivado's
    `-dict { … }` shorthand isn't supported by the open flow."""
    cfg = resolved["configuration"]
    pinmap = resolved["board_pinmap"]
    default_iostd = (pinmap.get("defaults") or {}).get("iostandard") or "LVCMOS33"

    out = []
    out.append("# =============================================================================")
    out.append("# Auto-generated XDC constraints (simple form, openxc7) — DO NOT EDIT")
    out.append("# Configuration: {}".format(cfg["id"]))
    out.append("# =============================================================================")
    out.append("")

    referenced = collect_referenced_banks(resolved)
    plans = build_capability_plans(resolved)

    def emit(pin, port, iostd):
        if "," in str(pin):
            out.append("# WARNING: pin '{}' for port '{}' is a differential pair (skipped)".format(pin, port))
            return
        out.append("set_property PACKAGE_PIN {} [get_ports {{{}}}]".format(pin, port))
        out.append("set_property IOSTANDARD {} [get_ports {{{}}}]".format(iostd, port))

    for bank_name in referenced:
        bank = (pinmap.get("pinBanks") or {}).get(bank_name)
        if bank is None:
            continue
        pins = bank.get("pins")
        overrides = bank.get("overrides") or {}
        bank_iostd = bank.get("iostandard") or default_iostd
        if isinstance(pins, str):
            emit(pins, bank_name, _pin_iostd(pins, overrides, bank_iostd))
        elif isinstance(pins, list):
            for i, p in enumerate(pins):
                if p is None:
                    continue
                emit(p, "{}[{}]".format(bank_name, i), _pin_iostd(p, overrides, bank_iostd))
        elif isinstance(pins, dict):
            for sub, val in pins.items():
                pname = "{}_{}".format(bank_name, sub)
                if isinstance(val, list):
                    for i, p in enumerate(val):
                        if p is None:
                            continue
                        emit(p, "{}[{}]".format(pname, i), _pin_iostd(p, overrides, bank_iostd))
                elif isinstance(val, str):
                    emit(val, pname, _pin_iostd(val, overrides, bank_iostd))

    # Clock create_clock entries
    out.append("")
    for pidx, perif, params in plans["clock"].providers:
        attach = resolved["peripherals"][pidx]
        clk_bank = (attach.get("bind") or {}).get("clk")
        if clk_bank is None:
            continue
        port = _bank_port_name(clk_bank)
        freq = (params or {}).get("frequency_mhz")
        if freq is None:
            m = re.search(r"(\d+)mhz", str(clk_bank).lower())
            if m:
                freq = int(m.group(1))
        if freq is None:
            continue
        period_ns = 1000.0 / float(freq)
        out.append("create_clock -name sys_clk_{f}mhz -period {p:.3f} [get_ports {{{port}}}]".format(
            f=int(freq), p=period_ns, port=port))

    out.append("")
    return "\n".join(out)


def _pin_iostd(pin, overrides, default):
    return overrides.get(pin) or default


def _bank_port_name(bank_ref):
    """Translate a bank reference like `clk100mhz` or `onboard_uart.tx` into the
    SV port name produced by emit_top_sv (which strips dots to underscores)."""
    m = _BANK_REF.match(str(bank_ref))
    if not m:
        return str(bank_ref)
    bank, sub, idx = m.group(1), m.group(2), m.group(3)
    name = bank if sub is None else "{}_{}".format(bank, sub)
    return name + ("[{}]".format(idx) if idx is not None else "")


# ---------------------------------------------------------------------------
# QSF / SDC constraint emission (Intel/Altera — Quartus Prime / Quartus II)
# ---------------------------------------------------------------------------

# Map BoardYaml.PartFamily strings to Quartus-canonical FAMILY assignment names.
# When a Cyclone IV part starts with EP4CGX/EP4CE we disambiguate the GX/E
# variants (Quartus rejects bare "Cyclone IV").
_QUARTUS_FAMILY = {
    "MAX 10":         "MAX 10",
    "Cyclone V SoC":  "Cyclone V",
    "Cyclone V":      "Cyclone V",
    "Cyclone IV E":   "Cyclone IV E",
    "Cyclone IV GX":  "Cyclone IV GX",
    "Cyclone III":    "Cyclone III",
    "Cyclone II":     "Cyclone II",
    "Cyclone":        "Cyclone",
    "MAX V":          "MAX V",
    "MAX II":         "MAX II",
    "MAX II CPLD":    "MAX II",
    "MAX V CPLD":     "MAX V",
}


def _quartus_family(board, part):
    """Pick a FAMILY string for the QSF. Disambiguate Cyclone IV by part prefix."""
    fam = (board.get("PartFamily") or "").strip()
    if fam == "Cyclone IV":
        p = (part or "").upper()
        if p.startswith("EP4CGX"):
            return "Cyclone IV GX"
        return "Cyclone IV E"
    return _QUARTUS_FAMILY.get(fam, fam)


def emit_qsf(resolved, part):
    """Emit a Quartus QSF settings file: family, device, top entity, plus
    per-pin set_location_assignment + IO_STANDARD lines for every referenced
    port. Mirrors emit_xdc's bank-walking logic."""
    cfg = resolved["configuration"]
    board = resolved["board"]
    pinmap = resolved["board_pinmap"]
    default_iostd = (pinmap.get("defaults") or {}).get("iostandard") or "3.3-V LVTTL"
    family = _quartus_family(board, part)

    out = []
    out.append("# =============================================================================")
    out.append("# Auto-generated QSF settings — DO NOT EDIT")
    out.append("# Configuration: {}".format(cfg["id"]))
    out.append("# Board:         {}".format(board.get("BoardName", board["Id"])))
    out.append("# =============================================================================")
    out.append("")
    out.append('set_global_assignment -name FAMILY "{}"'.format(family))
    out.append("set_global_assignment -name DEVICE {}".format(part))
    out.append("set_global_assignment -name TOP_LEVEL_ENTITY top")
    # Default Verilog input version: SystemVerilog 2005. Without this, Quartus
    # parses `.v` files (and `\\`include`d `.svh`/`.vh` headers) as Verilog 2001,
    # rejecting `'0`, `always_ff`, `logic`, etc.
    out.append("set_global_assignment -name VERILOG_INPUT_VERSION SYSTEMVERILOG_2005")
    out.append("")

    referenced = collect_referenced_banks(resolved)
    plans = build_capability_plans(resolved)

    for bank_name in referenced:
        bank = (pinmap.get("pinBanks") or {}).get(bank_name)
        if bank is None:
            out.append("# WARNING: bank '{}' referenced but not in pinBanks".format(bank_name))
            continue
        pins = bank.get("pins")
        overrides = bank.get("overrides") or {}
        bank_iostd = bank.get("iostandard") or default_iostd

        if isinstance(pins, str):
            out.extend(_qsf_lines(pins, bank_name, _pin_iostd(pins, overrides, bank_iostd)))
        elif isinstance(pins, list):
            for i, p in enumerate(pins):
                if p is None:
                    continue
                port = "{}[{}]".format(bank_name, i)
                out.extend(_qsf_lines(p, port, _pin_iostd(p, overrides, bank_iostd)))
        elif isinstance(pins, dict):
            for sub, val in pins.items():
                pname = "{}_{}".format(bank_name, sub)
                if isinstance(val, list):
                    for i, p in enumerate(val):
                        if p is None:
                            continue
                        port = "{}[{}]".format(pname, i)
                        out.extend(_qsf_lines(p, port, _pin_iostd(p, overrides, bank_iostd)))
                elif isinstance(val, str):
                    out.extend(_qsf_lines(val, pname, _pin_iostd(val, overrides, bank_iostd)))

    out.append("")
    return "\n".join(out) + "\n"


def _qsf_lines(pin, port_expr, iostd):
    pin_str = str(pin)
    if "," in pin_str:
        return ["# WARNING: pin '{}' for port '{}' is a differential pair (skipped)".format(pin_str, port_expr)]
    return [
        "set_location_assignment PIN_{pin} -to {port}".format(pin=pin_str, port=port_expr),
        'set_instance_assignment -name IO_STANDARD "{std}" -to {port}'.format(std=iostd, port=port_expr),
    ]


def emit_sdc(resolved):
    """Emit an SDC timing constraints file. Currently just create_clock entries
    for each clock-providing peripheral. Used by both Quartus and Gowin EDA."""
    cfg = resolved["configuration"]
    plans = build_capability_plans(resolved)

    out = []
    out.append("# =============================================================================")
    out.append("# Auto-generated SDC timing — DO NOT EDIT")
    out.append("# Configuration: {}".format(cfg["id"]))
    out.append("# =============================================================================")
    out.append("")

    for pidx, perif, params in plans["clock"].providers:
        attach = resolved["peripherals"][pidx]
        clk_bank = (attach.get("bind") or {}).get("clk")
        if clk_bank is None:
            continue
        port = _bank_port_name(clk_bank)
        freq = (params or {}).get("frequency_mhz")
        if freq is None:
            m = re.search(r"(\d+)mhz", str(clk_bank).lower())
            if m:
                freq = int(m.group(1))
        if freq is None:
            continue
        period_ns = 1000.0 / float(freq)
        out.append(
            "create_clock -name sys_clk_{f}mhz -period {p:.3f} "
            "[get_ports {{{port}}}]".format(f=int(freq), p=period_ns, port=port)
        )

    out.append("derive_pll_clocks -create_base_clocks")
    out.append("derive_clock_uncertainty")
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CST / SDC constraint emission (Gowin EDA)
# ---------------------------------------------------------------------------

# Map default IOSTANDARD strings between vendor dialects. Gowin's CST uses
# its own IO_TYPE keywords (LVCMOS33, LVDS25, etc.) — most of our pinmaps
# already store the Gowin form for Gowin boards, but normalize just in case.
_GOWIN_IOTYPE = {
    "LVCMOS33":      "LVCMOS33",
    "LVCMOS25":      "LVCMOS25",
    "LVCMOS18":      "LVCMOS18",
    "LVCMOS15":      "LVCMOS15",
    "LVCMOS12":      "LVCMOS12",
    "LVDS25":        "LVDS25",
    "3.3-V LVTTL":   "LVCMOS33",
    "LVTTL":         "LVCMOS33",
}


def emit_cst(resolved):
    """Emit a Gowin CST physical constraints file. IO_LOC for pin numbers and
    IO_PORT for IO_TYPE / drive strength."""
    cfg = resolved["configuration"]
    pinmap = resolved["board_pinmap"]
    default_iotype = _GOWIN_IOTYPE.get(
        (pinmap.get("defaults") or {}).get("iostandard"), "LVCMOS33")

    out = []
    out.append("// =============================================================================")
    out.append("// Auto-generated CST constraints — DO NOT EDIT")
    out.append("// Configuration: {}".format(cfg["id"]))
    out.append("// =============================================================================")
    out.append("")

    referenced = collect_referenced_banks(resolved)

    for bank_name in referenced:
        bank = (pinmap.get("pinBanks") or {}).get(bank_name)
        if bank is None:
            out.append("// WARNING: bank '{}' referenced but not in pinBanks".format(bank_name))
            continue
        pins = bank.get("pins")
        overrides = bank.get("overrides") or {}
        bank_iotype = _GOWIN_IOTYPE.get(bank.get("iostandard"), default_iotype)

        if isinstance(pins, str):
            out.extend(_cst_lines(pins, bank_name, _pin_iostd(pins, overrides, bank_iotype)))
        elif isinstance(pins, list):
            for i, p in enumerate(pins):
                if p is None:
                    continue
                port = "{}[{}]".format(bank_name, i)
                out.extend(_cst_lines(p, port, _pin_iostd(p, overrides, bank_iotype)))
        elif isinstance(pins, dict):
            for sub, val in pins.items():
                pname = "{}_{}".format(bank_name, sub)
                if isinstance(val, list):
                    for i, p in enumerate(val):
                        if p is None:
                            continue
                        port = "{}[{}]".format(pname, i)
                        out.extend(_cst_lines(p, port, _pin_iostd(p, overrides, bank_iotype)))
                elif isinstance(val, str):
                    out.extend(_cst_lines(val, pname, _pin_iostd(val, overrides, bank_iotype)))

    out.append("")
    return "\n".join(out)


def _cst_lines(pin, port_expr, iotype):
    pin_str = str(pin)
    if "," in pin_str:
        return ["// WARNING: pin '{}' for port '{}' is a differential pair (skipped)".format(pin_str, port_expr)]
    iotype_resolved = _GOWIN_IOTYPE.get(iotype, iotype)
    return [
        'IO_LOC  "{port}" {pin};'.format(port=port_expr, pin=pin_str),
        'IO_PORT "{port}" IO_TYPE={iot};'.format(port=port_expr, iot=iotype_resolved),
    ]


# ---------------------------------------------------------------------------
# LPF constraint emission (nextpnr-trellis — Lattice ECP5)
# ---------------------------------------------------------------------------

def emit_lpf(resolved):
    """Emit a Lattice Physical Constraint (.lpf) file for nextpnr-ecp5.
    Format per port:
        LOCATE COMP "<port>" SITE "<pin>";
        IOBUF PORT "<port>" IO_TYPE=<iotype>;
    Indexed bus elements use `port[idx]` syntax."""
    cfg = resolved["configuration"]
    pinmap = resolved["board_pinmap"]
    default_iotype = (pinmap.get("defaults") or {}).get("iostandard") or "LVCMOS33"
    # Normalize Gowin/Quartus-style iostandard names to ECP5/Lattice names.
    iotype_map = {
        "3.3-V LVTTL": "LVCMOS33",
        "LVTTL":       "LVCMOS33",
        "LVCMOS33":    "LVCMOS33",
        "LVCMOS25":    "LVCMOS25",
        "LVCMOS18":    "LVCMOS18",
        "LVCMOS15":    "LVCMOS15",
        "LVCMOS12":    "LVCMOS12",
    }
    default_iotype = iotype_map.get(default_iotype, default_iotype)

    out = []
    out.append("# =============================================================================")
    out.append("# Auto-generated LPF constraints — DO NOT EDIT")
    out.append("# Configuration: {}".format(cfg["id"]))
    out.append("# =============================================================================")
    out.append("")

    referenced = collect_referenced_banks(resolved)

    for bank_name in referenced:
        bank = (pinmap.get("pinBanks") or {}).get(bank_name)
        if bank is None:
            out.append("# WARNING: bank '{}' referenced but not in pinBanks".format(bank_name))
            continue
        pins = bank.get("pins")
        overrides = bank.get("overrides") or {}
        bank_iotype = iotype_map.get(bank.get("iostandard"), default_iotype)

        if isinstance(pins, str):
            out.extend(_lpf_lines(pins, bank_name, _pin_iostd(pins, overrides, bank_iotype), iotype_map))
        elif isinstance(pins, list):
            for i, p in enumerate(pins):
                if p is None:
                    continue
                port = "{}[{}]".format(bank_name, i)
                out.extend(_lpf_lines(p, port, _pin_iostd(p, overrides, bank_iotype), iotype_map))
        elif isinstance(pins, dict):
            for sub, val in pins.items():
                pname = "{}_{}".format(bank_name, sub)
                if isinstance(val, list):
                    for i, p in enumerate(val):
                        if p is None:
                            continue
                        port = "{}[{}]".format(pname, i)
                        out.extend(_lpf_lines(p, port, _pin_iostd(p, overrides, bank_iotype), iotype_map))
                elif isinstance(val, str):
                    out.extend(_lpf_lines(val, pname, _pin_iostd(val, overrides, bank_iotype), iotype_map))

    out.append("")
    return "\n".join(out)


def _lpf_lines(pin, port_expr, iotype, iotype_map):
    pin_str = str(pin)
    if "," in pin_str:
        return ['# WARNING: pin "{}" for port "{}" is a differential pair (skipped)'.format(pin_str, port_expr)]
    iotype_resolved = iotype_map.get(iotype, iotype)
    return [
        'LOCATE COMP "{port}" SITE "{pin}";'.format(port=port_expr, pin=pin_str),
        'IOBUF PORT "{port}" IO_TYPE={iot};'.format(port=port_expr, iot=iotype_resolved),
    ]


# ---------------------------------------------------------------------------
# Efinity peri.xml + project.xml emission (Efinix Trion / Titanium)
# ---------------------------------------------------------------------------

def _xml_attr(s):
    """Minimal XML-attribute escape."""
    return (str(s).replace("&", "&amp;").replace('"', "&quot;")
                  .replace("<", "&lt;").replace(">", "&gt;"))


def emit_peri_xml(resolved, device_def):
    """Emit Efinity's peripheral XML — one <efxpt:gpio> per top-level port,
    plus iobank/bus declarations and oscillator info for any virtual clock.
    Pin numbers are GPIOR_NN strings (Efinity-specific def names), not
    physical ball/pad numbers.

    `device_def`: e.g. "T8F81" — same string used in board.fpga.part."""
    cfg = resolved["configuration"]
    pinmap = resolved["board_pinmap"]
    default_iostd = (pinmap.get("defaults") or {}).get("iostandard") or "3.3 V LVTTL / LVCMOS"

    out = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>')
    out.append(
        '<efxpt:design_db name="{name}" device_def="{dev}" '
        'version="2023.2.307" db_version="20232999" '
        'xmlns:efxpt="http://www.efinixinc.com/peri_design_db" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://www.efinixinc.com/peri_design_db peri_design_db.xsd">'
        .format(name=_xml_attr(cfg["id"]), dev=_xml_attr(device_def))
    )

    # ---- iobank info (collected across all referenced banks) ----
    out.append("    <efxpt:device_info>")
    out.append("        <efxpt:iobank_info>")
    # T8F81 standard banks. Should match what BGM uses; if a board needs
    # something else, override via board_pinmap.iobanks.
    iobanks = (pinmap.get("iobanks") or {
        "1A": "3.3 V LVTTL / LVCMOS",
        "1B": "3.3 V LVTTL / LVCMOS",
        "1C": "1.1 V",
        "2A": "3.3 V LVTTL / LVCMOS",
        "2B": "3.3 V LVTTL / LVCMOS",
    })
    for name, iostd in iobanks.items():
        out.append('            <efxpt:iobank name="{}" iostd="{}"/>'.format(_xml_attr(name), _xml_attr(iostd)))
    out.append("        </efxpt:iobank_info>")
    out.append("    </efxpt:device_info>")

    # ---- gpio info ----
    referenced = collect_referenced_banks(resolved)
    plans = build_capability_plans(resolved)
    osc_clocks = []   # (osc-driven port_name, frequency_mhz) for the osc_info block

    out.append('    <efxpt:gpio_info device_def="{}">'.format(_xml_attr(device_def)))
    buses = []   # (bus_name, mode, msb, lsb)

    for bank_name in referenced:
        bank = (pinmap.get("pinBanks") or {}).get(bank_name)
        if bank is None:
            continue
        # Virtual oscillator-sourced clock — record it in osc_clocks
        # and skip the gpio entry.
        if bank.get("virtual"):
            if bank.get("source") == "osc":
                osc_clocks.append((bank_name, bank.get("frequency_mhz", 50)))
            continue

        pins = bank.get("pins")
        bank_iostd = bank.get("iostandard") or default_iostd
        direction = _infer_pin_direction(resolved, bank_name,
                                         None if not isinstance(pins, dict) else next(iter(pins)))
        mode = direction if direction in ("input", "output", "inout") else "input"

        if isinstance(pins, str):
            out.extend(_efxpt_gpio(bank_name, pins, mode, "", bank_iostd))
        elif isinstance(pins, list):
            buses.append((bank_name, mode, len(pins) - 1, 0))
            for i, p in enumerate(pins):
                if p is None:
                    continue
                portname = "{}[{}]".format(bank_name, i)
                out.extend(_efxpt_gpio(portname, p, mode, bank_name, bank_iostd))
        elif isinstance(pins, dict):
            for sub, val in pins.items():
                pname = "{}_{}".format(bank_name, sub)
                d = _infer_pin_direction(resolved, bank_name, sub)
                m = d if d in ("input", "output", "inout") else "input"
                if isinstance(val, list):
                    buses.append((pname, m, len(val) - 1, 0))
                    for i, p in enumerate(val):
                        if p is None:
                            continue
                        portname = "{}[{}]".format(pname, i)
                        out.extend(_efxpt_gpio(portname, p, m, pname, bank_iostd))
                elif isinstance(val, str):
                    out.extend(_efxpt_gpio(pname, val, m, "", bank_iostd))

    # Default unused-pin policy (same as BGM)
    out.append('        <efxpt:global_unused_config state="input with weak pullup"/>')

    for name, mode, msb, lsb in buses:
        out.append('        <efxpt:bus name="{}" mode="{}" msb="{}" lsb="{}"/>'
                   .format(_xml_attr(name), _xml_attr(mode), msb, lsb))
    out.append("    </efxpt:gpio_info>")

    # ---- oscillator info for virtual clocks ----
    out.append("    <efxpt:pll_info/>")
    if osc_clocks:
        out.append("    <efxpt:osc_info>")
        for clock_port, _freq in osc_clocks:
            out.append('        <efxpt:osc name="osc_inst1" osc_def="OSC_0" clock_name="{}"/>'
                       .format(_xml_attr(clock_port)))
        out.append("    </efxpt:osc_info>")
    else:
        out.append("    <efxpt:osc_info/>")
    out.append("    <efxpt:jtag_info/>")
    out.append("</efxpt:design_db>")
    return "\n".join(out) + "\n"


def _efxpt_gpio(port_name, gpio_def, mode, bus_name, iostd):
    """Render one <efxpt:gpio> element with its nested input_config /
    output_config / inout_config child."""
    lines = ['        <efxpt:gpio name="{}" gpio_def="{}" mode="{}" bus_name="{}" '
             'is_lvds_gpio="false" io_standard="{}">'.format(
                _xml_attr(port_name), _xml_attr(gpio_def), _xml_attr(mode),
                _xml_attr(bus_name), _xml_attr(iostd))]
    if mode == "input":
        lines.append('            <efxpt:input_config name="{}" name_ddio_lo="" '
                     'conn_type="normal" is_register="false" clock_name="" '
                     'is_clock_inverted="false" pull_option="weak pullup" '
                     'is_schmitt_trigger="false" ddio_type="none"/>'.format(_xml_attr(port_name)))
    elif mode == "output":
        lines.append('            <efxpt:output_config name="{}" name_ddio_lo="" '
                     'register_option="none" clock_name="" is_clock_inverted="false" '
                     'is_slew_rate="false" tied_option="none" ddio_type="none" '
                     'drive_strength="1"/>'.format(_xml_attr(port_name)))
    else:  # inout
        lines.append('            <efxpt:inout_config name="{}" name_ddio_lo="" '
                     'conn_type="normal" pull_option="weak pullup"/>'
                     .format(_xml_attr(port_name)))
    lines.append("        </efxpt:gpio>")
    return lines


def emit_efx_project_xml(resolved, device_def, sv_files, sdc_path, peri_path,
                         project_name="unifpga_top"):
    """Emit Efinity's project XML wrapper. Lists every SV/V source, plus the
    SDC and peri XML, and the standard synth/pnr/bitstream parameters."""
    cfg = resolved["configuration"]
    family = (resolved["board"].get("PartFamily") or "Trion").strip()
    out = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>')
    out.append(
        '<efx:project xmlns:efx="http://www.efinixinc.com/enf_proj" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'name="{name}" description="{name}" sw_version="2023.2.307" '
        'xsi:schemaLocation="http://www.efinixinc.com/enf_proj enf_proj.xsd">'
        .format(name=_xml_attr(project_name))
    )
    out.append("    <efx:device_info>")
    out.append('        <efx:family name="{}"/>'.format(_xml_attr(family)))
    out.append('        <efx:device name="{}"/>'.format(_xml_attr(device_def)))
    out.append('        <efx:timing_model name="C2"/>')
    out.append("    </efx:device_info>")
    out.append('    <efx:design_info def_veri_version="sv_09" def_vhdl_version="vhdl_2008">')
    out.append('        <efx:top_module name="top"/>')
    for sv in sv_files:
        ext = "system_verilog" if sv.endswith((".sv", ".svh")) else "verilog"
        out.append('        <efx:design_file name="{}" version="{}" library="default"/>'.format(
            _xml_attr(sv), ext))
    out.append('        <efx:top_vhdl_arch name=""/>')
    out.append("    </efx:design_info>")
    out.append("    <efx:constraint_info>")
    out.append('        <efx:sdc_file name="{}"/>'.format(_xml_attr(sdc_path)))
    out.append('        <efx:inter_file name=""/>')
    out.append("    </efx:constraint_info>")
    out.append("    <efx:sim_info/>")
    out.append("    <efx:misc_info/>")
    out.append('    <efx:synthesis tool_name="efx_map">')
    out.append('        <efx:param name="write_efx_verilog" value="on" value_type="e_bool"/>')
    out.append('        <efx:param name="opt_mode" value="speed" value_type="e_option"/>')
    out.append("    </efx:synthesis>")
    out.append('    <efx:place_and_route tool_name="efx_pnr">')
    out.append('        <efx:param name="verbose" value="off" value_type="e_bool"/>')
    out.append('        <efx:param name="load_delaym" value="on" value_type="e_bool"/>')
    out.append("    </efx:place_and_route>")
    out.append('    <efx:bitstream_generation tool_name="efx_pgm">')
    out.append('        <efx:param name="mode" value="active" value_type="e_option"/>')
    out.append('        <efx:param name="width" value="1" value_type="e_option"/>')
    out.append('        <efx:param name="oscillator_clock_divider" value="DIV8" value_type="e_option"/>')
    out.append('        <efx:param name="enable_roms" value="on" value_type="e_option"/>')
    out.append('        <efx:param name="io_weak_pullup" value="on" value_type="e_bool"/>')
    out.append("    </efx:bitstream_generation>")
    out.append("</efx:project>")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# PCF constraint emission (nextpnr-icestorm — Lattice iCE40)
# ---------------------------------------------------------------------------

def emit_pcf(resolved):
    """Emit a PCF (Physical Constraint File) for nextpnr-ice40.
    Format: `set_io -nowarn <port> <pin>` per top-level port. Indexed bus
    elements use `port[idx]` syntax — same as XDC/QSF."""
    cfg = resolved["configuration"]
    pinmap = resolved["board_pinmap"]

    out = []
    out.append("# =============================================================================")
    out.append("# Auto-generated PCF constraints — DO NOT EDIT")
    out.append("# Configuration: {}".format(cfg["id"]))
    out.append("# =============================================================================")
    out.append("")

    referenced = collect_referenced_banks(resolved)
    plans = build_capability_plans(resolved)

    for bank_name in referenced:
        bank = (pinmap.get("pinBanks") or {}).get(bank_name)
        if bank is None:
            out.append("# WARNING: bank '{}' referenced but not in pinBanks".format(bank_name))
            continue
        pins = bank.get("pins")

        if isinstance(pins, str):
            out.append("set_io -nowarn {} {}".format(bank_name, pins))
        elif isinstance(pins, list):
            for i, p in enumerate(pins):
                if p is None:
                    continue
                out.append("set_io -nowarn {}[{}] {}".format(bank_name, i, p))
        elif isinstance(pins, dict):
            for sub, val in pins.items():
                pname = "{}_{}".format(bank_name, sub)
                if isinstance(val, list):
                    for i, p in enumerate(val):
                        if p is None:
                            continue
                        out.append("set_io -nowarn {}[{}] {}".format(pname, i, p))
                elif isinstance(val, str):
                    out.append("set_io -nowarn {} {}".format(pname, val))

    # Frequency hints for the timing analyzer (optional but cheap).
    out.append("")
    for pidx, perif, params in plans["clock"].providers:
        attach = resolved["peripherals"][pidx]
        clk_bank = (attach.get("bind") or {}).get("clk")
        if clk_bank is None:
            continue
        port = _bank_port_name(clk_bank)
        freq = (params or {}).get("frequency_mhz")
        if freq is None:
            m = re.search(r"(\d+)mhz", str(clk_bank).lower())
            if m:
                freq = int(m.group(1))
        if freq is not None:
            out.append("set_frequency {} {}".format(port, freq))

    out.append("")
    return "\n".join(out)


def emit_ccf(resolved):
    """Emit a CCF (Cologne Chip Constraints File) for nextpnr-himbaechel
    with the gatemate uarch. Format per the himbaechel ccf.cc parser:

        Pin_in   "<port>" LOC=<pad>;
        Pin_out  "<port>" LOC=<pad>;
        Pin_inout "<port>" LOC=<pad>;
        NET      "<port>" LOC=<pad>;     # equivalent to Pin_*

    Both `//` and `#` are comment markers. Lines must end with `;`. We
    keep the directive `Pin_*` (unlike the more generic NET) because the
    parser uses it for direction sanity-checking.
    """
    cfg = resolved["configuration"]
    pinmap = resolved["board_pinmap"]

    out = []
    out.append("# =============================================================================")
    out.append("# Auto-generated CCF constraints — DO NOT EDIT")
    out.append("# Configuration: {}".format(cfg["id"]))
    out.append("# Board:         {}".format((resolved.get("board") or {}).get("BoardName", "")))
    out.append("# =============================================================================")
    out.append("")

    referenced = collect_referenced_banks(resolved)

    def _emit(port, pad):
        # Pin_in vs Pin_out gets resolved by nextpnr from the netlist; the
        # generic `NET` directive matches whichever direction the cell has,
        # which is simplest to emit from a codegen that doesn't track
        # direction per port.
        out.append('NET "{port}" LOC={pad};'.format(port=port, pad=pad))

    for bank_name in referenced:
        bank = (pinmap.get("pinBanks") or {}).get(bank_name)
        if bank is None:
            out.append("# WARNING: bank '{}' referenced but not in pinBanks".format(bank_name))
            continue
        pins = bank.get("pins")

        if isinstance(pins, str):
            _emit(bank_name, pins)
        elif isinstance(pins, list):
            for i, p in enumerate(pins):
                if p is None:
                    continue
                _emit("{}[{}]".format(bank_name, i), p)
        elif isinstance(pins, dict):
            for sub, val in pins.items():
                pname = "{}_{}".format(bank_name, sub)
                if isinstance(val, list):
                    for i, p in enumerate(val):
                        if p is None:
                            continue
                        _emit("{}[{}]".format(pname, i), p)
                elif isinstance(val, str):
                    _emit(pname, val)

    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def generate_for(configuration_id):
    resolved = config_init.resolve_configuration(configuration_id)
    return emit_top_sv(resolved)


def generate_xdc_for(configuration_id):
    resolved = config_init.resolve_configuration(configuration_id)
    return emit_xdc(resolved)


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("configuration", help="Configuration id (or path to file)")
    p.add_argument("-o", "--output", help="Write to file instead of stdout")
    args = p.parse_args(argv)

    text = generate_for(args.configuration)
    if args.output:
        with open(args.output, "w") as f:
            f.write(text)
        print("Wrote {}".format(args.output))
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
