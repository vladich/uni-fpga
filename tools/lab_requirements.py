"""
Parse capability requirements declared at the top of a `lab_top.sv` file and
validate them against a resolved configuration.

The user writes a comment block like

    // requires:
    //   switches >= 4
    //   leds      >= 4
    //   buttons   >= 2
    //   screen    >= 640x480
    //   audio_in
    //   serial_console
    //   gpio      >= 16

before the `module lab_top` line. Whitespace is free; ordering doesn't matter;
each line names exactly one capability with an optional `>=` constraint.

Constraint grammar:
    <capability>                       # presence only
    <capability> >= <integer>          # width-parameterized capabilities
    <capability> >= <W>x<H>            # screen
    <capability> >= <W>x<H>@<depth>    # screen with required color depth (444/565/888)

Validation against a resolved bundle:
    check(resolved, requirements) -> list[str]    # errors; [] if all OK
"""

import os
import re
from collections import OrderedDict


# Capability primary parameter names — what `>= N` constrains for each.
_CAP_WIDTH_PARAM = {
    "switches":      "width",
    "buttons":       "width",
    "leds":          "width",
    "rgb_leds":      "count",
    "seven_segment": "digits",
    "gpio":          "width",
}

_PRESENCE_ONLY = {"clock", "reset", "audio_in", "audio_out", "serial_console"}


_REQ_LINE = re.compile(
    r"^\s*//\s*"
    r"(?P<cap>[a-z_][a-z0-9_]*)"
    r"(?:\s*>=\s*(?P<spec>.+?))?"
    r"\s*$",
    re.IGNORECASE,
)


def parse(sv_path):
    """Parse a lab_top.sv file and return an OrderedDict of {cap_id: {...}}.

    Supported entries:
      {min_width: int}                  — for width-parameterized caps
      {min_width: int, min_height: int, min_color_depth: int}  — for screen
      {}                                 — presence only
    Returns empty if no `requires:` block is found.
    """
    if not os.path.exists(sv_path):
        return OrderedDict()
    with open(sv_path) as f:
        text = f.read()

    # Find the requires: block. It's a sequence of `// ...` lines starting with
    # a `// requires:` line and ending at the first non-comment line.
    lines = text.splitlines()
    in_block = False
    out = OrderedDict()
    for line in lines:
        if not in_block:
            if re.match(r"^\s*//\s*requires\s*:\s*$", line, re.IGNORECASE):
                in_block = True
            continue
        # Stop at first line that isn't a `//` comment.
        if not re.match(r"^\s*//", line):
            break
        # Skip blank-comment lines like `// `
        m = _REQ_LINE.match(line)
        if not m:
            continue
        cap = m.group("cap").lower()
        if cap == "requires":
            continue   # the header line we already consumed
        spec = (m.group("spec") or "").strip()
        out[cap] = _parse_spec(cap, spec)
    return out


def _parse_spec(cap, spec):
    if not spec:
        return {}
    # screen-style: WxH or WxH@depth
    m = re.match(r"^(\d+)\s*x\s*(\d+)(?:\s*@\s*(\d+))?$", spec, re.IGNORECASE)
    if m:
        out = {"min_width": int(m.group(1)), "min_height": int(m.group(2))}
        if m.group(3):
            out["min_color_depth"] = int(m.group(3))
        return out
    # plain integer
    if spec.isdigit():
        return {"min_width": int(spec)}
    raise ValueError("Cannot parse requirement spec for {c}: {s!r}".format(c=cap, s=spec))


def check(resolved, requirements, capabilities=None):
    """Compare resolved configuration against parsed requirements.
    Returns a list of error strings — empty when every requirement is met."""
    if capabilities is None:
        from config import init as config_init
        capabilities = config_init.read_capabilities()

    # Build per-capability summaries from the resolved bundle.
    cap_summary = _summarize_capabilities(resolved, capabilities)

    errors = []
    for cap_id, req in requirements.items():
        if cap_id not in capabilities:
            errors.append("lab_top requires unknown capability '{}'".format(cap_id))
            continue
        provided = cap_summary.get(cap_id)
        if provided is None or provided["providers"] == 0:
            errors.append(
                "Configuration '{c}' does not provide capability '{cap}'"
                .format(c=resolved["configuration"]["id"], cap=cap_id)
            )
            continue
        # Width-parameterized capabilities
        param = _CAP_WIDTH_PARAM.get(cap_id)
        if param and "min_width" in req:
            actual = provided.get(param) or 0
            if actual < req["min_width"]:
                errors.append(
                    "Configuration '{c}': capability '{cap}' provides {p}={a}, "
                    "lab_top requires >= {n}"
                    .format(c=resolved["configuration"]["id"], cap=cap_id,
                            p=param, a=actual, n=req["min_width"])
                )
        # Screen-specific
        if cap_id == "screen":
            if req.get("min_width") and provided.get("width", 0) < req["min_width"]:
                errors.append(
                    "Configuration '{c}': screen is {w}x{h}, lab_top requires {rw}x{rh}"
                    .format(c=resolved["configuration"]["id"],
                            w=provided.get("width"), h=provided.get("height"),
                            rw=req["min_width"], rh=req.get("min_height", 0))
                )
            if req.get("min_height") and provided.get("height", 0) < req["min_height"]:
                errors.append(
                    "Configuration '{c}': screen height {h} < required {rh}"
                    .format(c=resolved["configuration"]["id"],
                            h=provided.get("height"), rh=req["min_height"])
                )
            if req.get("min_color_depth"):
                # Treat 444/565/888 as ordered integers.
                if (provided.get("color_depth") or 0) < req["min_color_depth"]:
                    errors.append(
                        "Configuration '{c}': screen color_depth {a} < required {n}"
                        .format(c=resolved["configuration"]["id"],
                                a=provided.get("color_depth"), n=req["min_color_depth"])
                    )
    return errors


def _summarize_capabilities(resolved, capabilities):
    """For each capability, sum widths over providers (or pick exclusive params).
    Falls back to the peripheral's parameter defaults when the configuration
    doesn't override them."""
    summary = OrderedDict()
    for cap_id in capabilities:
        summary[cap_id] = {"providers": 0}
    for attach in resolved["peripherals"]:
        perif = attach["peripheral"]
        params = attach.get("params") or {}
        perif_param_defs = (perif.get("parameters") or {})
        for entry in perif.get("provides") or []:
            cap_id = entry["capability"]
            cap = capabilities[cap_id]
            agg = cap.get("aggregation")
            cap_params = entry.get("params") or {}
            resolved_cap_params = {}
            for k, v in cap_params.items():
                if isinstance(v, str) and v.startswith("$"):
                    pname = v[1:]
                    val = params.get(pname)
                    if val is None:
                        val = (perif_param_defs.get(pname) or {}).get("default")
                    resolved_cap_params[k] = val
                else:
                    resolved_cap_params[k] = v
            s = summary[cap_id]
            s["providers"] += 1
            if agg == "concat":
                primary = _CAP_WIDTH_PARAM.get(cap_id, "width")
                w = resolved_cap_params.get(primary) or 1
                s[primary] = (s.get(primary) or 0) + w
            else:
                for k, v in resolved_cap_params.items():
                    if v is not None and k not in s:
                        s[k] = v
    return summary
