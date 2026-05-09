"""
YAML smoke tests. These catch the class of bugs found while cleaning up the
repo: mismatched PartFamily strings between boards.yml and parts.yml, fake
chip families, missing-comma typos that silently merge two pin names into
one, toolchain ids that don't resolve to a Python module, configurations
that reference unknown peripherals, etc.

Run with:  python -m pytest tests/
Or stand-alone (pytest not required) via the script at the bottom.
"""

import importlib
import os
import re

import yaml

try:
    import pytest
except ImportError:                                    # pragma: no cover
    pytest = None

from config import init as config_init


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
# Vendor-agnostic pin shape: letters+digits ("E3", "K17"), or letters-only
# ("AN" on QMtech), or pure digits ("35" on iCE40 PCF), or differential
# pair "P,N" ("H5,J5" on Gowin).
_PIN_TOKEN = re.compile(r"^[A-Za-z]+\d*$|^\d+$")


# ---------------------------------------------------------------------------
# Catalog round-trip and cross-reference tests
# ---------------------------------------------------------------------------

def _boards():        return config_init.read_boards_catalog()
def _toolchains():    return config_init.read_toolchains()
def _parts():         return config_init.read_parts()
def _peripherals():   return config_init.read_peripherals()
def _capabilities():  return config_init.read_capabilities()
def _configurations(): return config_init.read_configurations()


# ---------------------------------------------------------------------------
# Capability catalog
# ---------------------------------------------------------------------------

_VALID_DIRECTIONS = {"hw_to_user", "user_to_hw", "inout"}
_VALID_AGGREGATION = {"concat", "exclusive", "or", "mux"}
_VALID_SIGNAL_TYPES = {"scalar", "bus"}


def test_capabilities_have_required_fields():
    caps = _capabilities()
    assert caps, "No capabilities under config/capabilities/"
    for cap_id, cap in caps.items():
        assert "id" in cap, "Capability {c}: missing id".format(c=cap_id)
        assert "signals" in cap, "Capability {c}: missing signals".format(c=cap_id)
        assert "aggregation" in cap, "Capability {c}: missing aggregation".format(c=cap_id)
        assert cap["aggregation"] in _VALID_AGGREGATION, (
            "Capability {c}: aggregation {a!r} is not one of {v}"
            .format(c=cap_id, a=cap["aggregation"], v=sorted(_VALID_AGGREGATION))
        )


def test_capability_signals_well_formed():
    for cap_id, cap in _capabilities().items():
        for sig in cap["signals"]:
            assert "name" in sig, "Capability {c}: signal without name".format(c=cap_id)
            assert "type" in sig, ("Capability {c}: signal {s} has no type"
                                    .format(c=cap_id, s=sig.get("name")))
            assert sig["type"] in _VALID_SIGNAL_TYPES, (
                "Capability {c}.{s}: type {t!r} is not one of {v}"
                .format(c=cap_id, s=sig["name"], t=sig["type"], v=sorted(_VALID_SIGNAL_TYPES))
            )
            assert "direction" in sig, ("Capability {c}: signal {s} has no direction"
                                         .format(c=cap_id, s=sig["name"]))
            assert sig["direction"] in _VALID_DIRECTIONS, (
                "Capability {c}.{s}: direction {d!r} is not one of {v}"
                .format(c=cap_id, s=sig["name"], d=sig["direction"], v=sorted(_VALID_DIRECTIONS))
            )


def test_boards_have_required_fields():
    boards = _boards()
    required = {"Id", "BoardName", "BoardProducer", "PartProducer", "PartFamily"}
    for board_id, board in boards.items():
        missing = required - set(board.keys())
        assert not missing, "Board {b} missing fields: {m}".format(b=board_id, m=missing)


def test_every_board_partfamily_resolves():
    boards = _boards()
    parts = _parts()
    for board_id, board in boards.items():
        producer = board["PartProducer"]
        family = board["PartFamily"]
        assert producer in parts, \
            "Board {b}: PartProducer {p!r} not in parts.yml".format(b=board_id, p=producer)
        assert family in parts[producer], \
            "Board {b}: PartFamily {f!r} not in parts.yml under producer {p!r}".format(
                b=board_id, f=family, p=producer)


def test_every_part_toolchain_is_declared():
    parts = _parts()
    toolchains = _toolchains()
    for producer, families in parts.items():
        for family, family_toolchains in families.items():
            for tcid in family_toolchains:
                assert tcid in toolchains, (
                    "parts.yml {p}/{f} references unknown toolchain id {t!r}"
                    .format(p=producer, f=family, t=tcid)
                )


def test_every_toolchain_id_has_a_module():
    toolchains = _toolchains()
    for tcid in toolchains:
        module_path = os.path.join(REPO_ROOT, "toolchains", tcid, tcid + ".py")
        assert os.path.exists(module_path), \
            "Toolchain {t}: missing module file {p}".format(t=tcid, p=module_path)
        mod = importlib.import_module("toolchains.{t}.{t}".format(t=tcid))
        assert hasattr(mod, "synthesize"), \
            "Toolchain {t}: module has no synthesize()".format(t=tcid)


# ---------------------------------------------------------------------------
# Per-board YAML structure (new pinBanks schema)
# ---------------------------------------------------------------------------

def test_per_board_yamls_have_pinbanks():
    """Every config/boards/<id>.yml must parse and expose Board.pinBanks."""
    boards_dir = os.path.join(REPO_ROOT, "config", "boards")
    files = [f for f in os.listdir(boards_dir)
             if f.endswith(".yml") and not f.startswith("_")]
    assert files, "No per-board YAML files found under config/boards/"

    for fname in sorted(files):
        path = os.path.join(boards_dir, fname)
        with open(path) as f:
            data = yaml.safe_load(f)
        assert data and "Board" in data, "{p} has no 'Board' root".format(p=path)
        board = data["Board"]
        assert "id" in board, "{p}: Board.id missing".format(p=path)
        assert "pinBanks" in board, "{p}: Board.pinBanks missing".format(p=path)


def test_pin_bank_pins_well_formed():
    """Walk every pinBank's pin values and reject anything that's clearly malformed
    (embedded spaces, empty strings, etc.). Catches missing-comma YAML typos."""
    boards_dir = os.path.join(REPO_ROOT, "config", "boards")
    for fname in sorted(os.listdir(boards_dir)):
        if not fname.endswith(".yml") or fname.startswith("_"):
            continue
        path = os.path.join(boards_dir, fname)
        with open(path) as f:
            data = yaml.safe_load(f)
        board_id = data["Board"]["id"]
        pin_banks = data["Board"].get("pinBanks", {}) or {}
        for bank_name, bank in pin_banks.items():
            pins = (bank or {}).get("pins") if isinstance(bank, dict) else None
            if pins is None:
                continue
            for pin_value in _walk_pin_values(pins):
                _assert_pin_token(board_id, bank_name, pin_value)


def _walk_pin_values(pins):
    """Yield every individual pin string (or int) from a pinBank.pins value,
    flattening lists and sub-key maps."""
    if pins is None:
        return
    if isinstance(pins, (str, int)):
        yield pins
    elif isinstance(pins, list):
        for el in pins:
            yield from _walk_pin_values(el)
    elif isinstance(pins, dict):
        for v in pins.values():
            yield from _walk_pin_values(v)


def _assert_pin_token(board_id, bank, pin_value):
    s = str(pin_value)
    assert s, "{b}.{n}: empty pin".format(b=board_id, n=bank)
    assert " " not in s, (
        "{b}.{n}: pin {p!r} contains a space — likely a missing comma in the YAML list"
        .format(b=board_id, n=bank, p=s)
    )
    # Allow simple pin tokens (E3, AN, 35) and Gowin diff pairs (H5,J5).
    parts = s.split(",")
    for p in parts:
        assert _PIN_TOKEN.match(p), (
            "{b}.{n}: pin {p!r} does not look like a valid pin name"
            .format(b=board_id, n=bank, p=p)
        )


# ---------------------------------------------------------------------------
# Peripheral catalog
# ---------------------------------------------------------------------------

def test_peripherals_have_required_fields():
    for pid, p in _peripherals().items():
        assert "id" in p, "Peripheral {p}: missing id".format(p=pid)
        assert "signals" in p, "Peripheral {p}: missing signals".format(p=pid)
        for sig in p["signals"]:
            assert "name" in sig, "Peripheral {p}: signal without a name".format(p=pid)
            assert "type" in sig, ("Peripheral {p}: signal {s} has no type"
                                   .format(p=pid, s=sig.get("name")))
        # New schema: provides + driver are required (provides may be []).
        assert "provides" in p, "Peripheral {p}: missing 'provides' (use [] if none)".format(p=pid)
        assert "driver" in p,   "Peripheral {p}: missing 'driver' (use null for passthrough)".format(p=pid)


def test_every_peripheral_provides_known_capabilities():
    caps = _capabilities()
    for pid, p in _peripherals().items():
        for entry in p.get("provides") or []:
            cap_id = entry.get("capability")
            assert cap_id in caps, (
                "Peripheral {p}: provides unknown capability {c!r}".format(p=pid, c=cap_id)
            )


_VALID_CONTEXT_REFS = {"clk", "rst", "rst_n", "clk_mhz"}


def test_peripheral_drivers_reference_existing_files():
    for pid, p in _peripherals().items():
        drv = p.get("driver")
        if drv is None:
            continue
        assert "module" in drv, "Peripheral {p}: driver has no module".format(p=pid)
        assert "file" in drv,   "Peripheral {p}: driver has no file".format(p=pid)
        assert "port_map" in drv, "Peripheral {p}: driver has no port_map".format(p=pid)
        # The SV file must exist on disk so codegen can compile it.
        path = os.path.join(REPO_ROOT, drv["file"])
        assert os.path.exists(path), (
            "Peripheral {p}: driver file {f} does not exist".format(p=pid, f=drv["file"])
        )
        # Sanity check: the file declares the named module.
        with open(path) as f:
            text = f.read()
        assert ("module " + drv["module"]) in text, (
            "Peripheral {p}: file {f} does not declare module '{m}'"
            .format(p=pid, f=drv["file"], m=drv["module"])
        )


def _validate_ref(pid, where, ref, pin_names, caps, provided):
    """Validate a single port_map / pin_assigns reference. Allows leading '~'
    (combinational invert) and indexed expressions like pin.d_p[0]."""
    assert isinstance(ref, str), (
        "Peripheral {p}: {w} is not a string ({r!r})"
        .format(p=pid, w=where, r=ref)
    )
    expr = ref.lstrip("~ ").strip()
    # Strip a single trailing index like [0..N].
    expr_root = expr.split("[", 1)[0]
    if expr_root.startswith("pin."):
        pin = expr_root[len("pin."):]
        assert pin in pin_names, (
            "Peripheral {p}: {w} references pin '{pin}' not in signals"
            .format(p=pid, w=where, pin=pin)
        )
    elif expr_root.startswith("capability."):
        rest = expr_root[len("capability."):]
        cap_id = rest.split(".", 1)[0]
        assert cap_id in caps, (
            "Peripheral {p}: {w} references unknown capability '{c}'"
            .format(p=pid, w=where, c=cap_id)
        )
        assert cap_id in provided, (
            "Peripheral {p}: {w} uses capability '{c}' but the peripheral does "
            "not declare it under provides:".format(p=pid, w=where, c=cap_id)
        )
    elif expr_root.startswith("context."):
        ctx = expr_root[len("context."):]
        assert ctx in _VALID_CONTEXT_REFS, (
            "Peripheral {p}: {w} references unknown context '{c}'"
            .format(p=pid, w=where, c=ctx)
        )
    elif expr_root.startswith("const."):
        pass
    elif expr_root == "":
        # A blank RHS in port_map means "leave port unconnected; codegen handles
        # via pin_assigns or with a wire". Allow it.
        pass
    else:
        raise AssertionError(
            "Peripheral {p}: {w} = {r!r} does not start with pin./capability./context./const."
            .format(p=pid, w=where, r=ref)
        )


def test_peripheral_port_map_references_well_formed():
    caps = _capabilities()
    for pid, p in _peripherals().items():
        drv = p.get("driver")
        if drv is None:
            continue
        pin_names = {sig["name"] for sig in p.get("signals", [])}
        provided = {entry["capability"] for entry in p.get("provides") or []}
        for port, ref in (drv.get("port_map") or {}).items():
            if ref is None:
                continue   # port intentionally unconnected
            _validate_ref(pid, "port_map[{}]".format(port), ref, pin_names, caps, provided)


def test_peripheral_pin_assigns_well_formed():
    """pin_assigns are direct combinational connections that bypass the driver
    instance — RHS must use the same vocabulary as port_map."""
    caps = _capabilities()
    for pid, p in _peripherals().items():
        pa = p.get("pin_assigns") or {}
        if not pa:
            continue
        pin_names = {sig["name"] for sig in p.get("signals", [])}
        provided = {entry["capability"] for entry in p.get("provides") or []}
        for lhs, rhs in pa.items():
            # LHS must be a valid pin / capability sink.
            _validate_ref(pid, "pin_assigns[{}].lhs".format(lhs), lhs, pin_names, caps, provided)
            _validate_ref(pid, "pin_assigns[{}].rhs".format(lhs), rhs, pin_names, caps, provided)


# ---------------------------------------------------------------------------
# Configuration resolution
# ---------------------------------------------------------------------------

def test_every_configuration_resolves():
    """Every config/configurations/*.yml must resolve cleanly: board exists,
    toolchain exists and is compatible, every attached peripheral exists."""
    configurations = _configurations()
    assert configurations, "No configurations under config/configurations/"
    for cfg_id in sorted(configurations):
        # resolve_configuration raises ConfigError on any inconsistency.
        config_init.resolve_configuration(cfg_id)


def test_every_configuration_has_known_toolchain():
    toolchains = _toolchains()
    for cfg_id, cfg in _configurations().items():
        tc = cfg.get("toolchain")
        assert tc in toolchains, (
            "Configuration {c}: toolchain {t!r} not in toolchains.yml"
            .format(c=cfg_id, t=tc)
        )


def test_every_configuration_has_known_peripherals():
    peripherals = _peripherals()
    for cfg_id, cfg in _configurations().items():
        for entry in cfg.get("attach", []) or []:
            pid = entry.get("peripheral")
            assert pid in peripherals, (
                "Configuration {c}: unknown peripheral {p!r}"
                .format(c=cfg_id, p=pid)
            )


# ---------------------------------------------------------------------------
# Codegen smoke test
# ---------------------------------------------------------------------------

def test_codegen_runs_for_every_configuration():
    """Run the SV codegen on every configuration. The output is not validated
    for synthesizability here, only that codegen completes without error and
    produces something that looks like a SV module."""
    from tools import codegen
    for cfg_id in sorted(_configurations()):
        try:
            text = codegen.generate_for(cfg_id)
        except Exception as exc:
            raise AssertionError("codegen failed for {c}: {e}".format(c=cfg_id, e=exc))
        assert "module top" in text, "codegen for {c}: no 'module top'".format(c=cfg_id)
        assert "endmodule" in text, "codegen for {c}: no 'endmodule'".format(c=cfg_id)
        assert "design_top" in text, "codegen for {c}: no design_top instantiation".format(c=cfg_id)


# ---------------------------------------------------------------------------
# Capability-requirements parser
# ---------------------------------------------------------------------------

def test_design_requirements_parser():
    from tools import design_requirements
    import tempfile
    sample = """\
// Some preamble
// requires:
//   switches >= 4
//   leds     >= 4
//   buttons  >= 2
//   screen   >= 640x480
//   audio_in
//   serial_console

module design_top (
    input clk
);
endmodule
"""
    with tempfile.NamedTemporaryFile("w", suffix=".sv", delete=False) as f:
        f.write(sample)
        p = f.name
    try:
        reqs = design_requirements.parse(p)
    finally:
        os.unlink(p)
    assert "switches" in reqs and reqs["switches"] == {"min_width": 4}, reqs
    assert "leds"     in reqs and reqs["leds"]     == {"min_width": 4}, reqs
    assert "buttons"  in reqs and reqs["buttons"]  == {"min_width": 2}, reqs
    assert "screen"   in reqs and reqs["screen"]   == {"min_width": 640, "min_height": 480}, reqs
    assert "audio_in" in reqs and reqs["audio_in"] == {}, reqs
    assert "serial_console" in reqs and reqs["serial_console"] == {}, reqs


def test_design_requirements_check_passes_when_satisfied():
    from tools import design_requirements
    resolved = config_init.resolve_configuration("nexys4_ddr_default")
    reqs = {
        "switches": {"min_width": 8},
        "leds":     {"min_width": 8},
        "buttons":  {"min_width": 3},
        "audio_in": {},
    }
    errs = design_requirements.check(resolved, reqs)
    assert errs == [], errs


def test_design_requirements_check_fails_when_under_provisioned():
    from tools import design_requirements
    resolved = config_init.resolve_configuration("nexys4_ddr_default")
    # Nexys 4 DDR has 16 switches; 32 should fail.
    reqs = {"switches": {"min_width": 32}}
    errs = design_requirements.check(resolved, reqs)
    assert len(errs) == 1 and "switches" in errs[0], errs


def test_design_requirements_check_fails_when_capability_missing():
    from tools import design_requirements
    # de10_lite has no audio_out (no PWM amp on board).
    resolved = config_init.resolve_configuration("de10_lite")
    reqs = {"audio_out": {}}
    errs = design_requirements.check(resolved, reqs)
    assert len(errs) == 1 and "audio_out" in errs[0], errs


# ---------------------------------------------------------------------------
# Stand-alone runner so we can validate without pytest installed
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in funcs:
        try:
            fn()
            print("ok    {}".format(fn.__name__))
        except Exception as exc:
            failures += 1
            print("FAIL  {}: {}".format(fn.__name__, exc))
    sys.exit(1 if failures else 0)
