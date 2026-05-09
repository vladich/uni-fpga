"""
nextpnr-trellis toolchain driver (yosys + nextpnr-ecp5 + ecppack).

Pipeline:
    yosys -p "read_verilog -sv …; synth_ecp5 -top top -json out.json"
    nextpnr-ecp5 --<chip> --package <pkg> --json in.json --lpf in.lpf
                 --textcfg out.config --lpf-allow-unconstrained
    ecppack out.config out.bit

Artifacts:
    <output>/top.sv             — codegen-produced top module
    <output>/unifpga_top.lpf    — codegen-produced LPF
    <output>/yosys.log          — yosys log
    <output>/nextpnr.log        — nextpnr log
    <output>/unifpga_top.json   — yosys netlist
    <output>/unifpga_top.config — nextpnr ascii placed-and-routed
    <output>/unifpga_top.bit    — final bitstream (ecppack output)

Set $UNIFPGA_DRY_RUN=1 to generate every artifact without running tools.
"""

import logging
import os
import re
import shutil
import subprocess

from tools import codegen


log = logging.getLogger(__name__)
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

PROJECT_NAME = "unifpga_top"


_OSS_CAD = os.path.expanduser("~/oss-cad-suite/bin")


def _resolve_bin(name):
    """Prefer ~/oss-cad-suite/bin (newer yosys 0.41+) before falling back
    to $PATH."""
    cand = os.path.join(_OSS_CAD, name)
    if os.path.exists(cand) and os.access(cand, os.X_OK):
        return cand
    return shutil.which(name)


def _collect_sv_sources(repo, peripherals, user_design_top, generated_top):
    """Same pattern as the icestorm driver — yosys 0.36 frontend rejects
    multi-dim packed arrays, so gate helper inclusion by module-name match."""
    files = [generated_top, os.path.abspath(user_design_top)]
    seen = {os.path.abspath(p) for p in files}

    design_dir = os.path.dirname(os.path.abspath(user_design_top))
    if os.path.isdir(design_dir):
        for root, _dirs, names in os.walk(design_dir):
            for name in sorted(names):
                if not (name.endswith(".sv") or name.endswith(".v")):
                    continue
                if name in ("design_top.sv", "tb.sv"):
                    continue
                full = os.path.join(root, name)
                if full not in seen:
                    files.append(full)
                    seen.add(full)

    for attach in peripherals:
        drv = (attach.get("peripheral") or {}).get("driver") or {}
        f = drv.get("file")
        if f:
            full = os.path.join(repo, f)
            if os.path.exists(full) and full not in seen:
                files.append(full)
                seen.add(full)

    try:
        with open(generated_top) as f:
            top_text = f.read()
    except Exception:
        top_text = ""

    helper_modules = {
        "tm1638_registers.sv":         ("tm1638_registers", "tm1638_board_controller"),
        "slow_clk_gen.sv":             ("slow_clk_gen",),
        "imitate_reset_on_power_up.sv": ("imitate_reset_on_power_up",),
    }
    for helper, modules in helper_modules.items():
        full = os.path.join(repo, "peripherals", helper)
        if not os.path.exists(full) or full in seen:
            continue
        if any(m in top_text for m in modules):
            files.append(full)
            seen.add(full)

    sibling_text = top_text
    for f in list(files):
        try:
            with open(f) as fh:
                sibling_text += "\n" + fh.read()
        except Exception:
            pass

    designs_common_dir = os.path.join(repo, "peripherals", "designs_common")
    if os.path.isdir(designs_common_dir):
        for name in sorted(os.listdir(designs_common_dir)):
            if not name.endswith(".sv"):
                continue
            module_name = name[:-3]
            if module_name not in sibling_text:
                continue
            full = os.path.join(designs_common_dir, name)
            if full not in seen:
                files.append(full)
                seen.add(full)

    # Xilinx-primitive stubs (BUFG etc.) for designs that target Vivado directly.
    compat_dir = os.path.join(repo, "peripherals", "_quartus_compat")
    if os.path.isdir(compat_dir):
        for name in sorted(os.listdir(compat_dir)):
            if not name.endswith(".sv"):
                continue
            full = os.path.join(compat_dir, name)
            if full not in seen:
                files.append(full)
                seen.add(full)

    return files


# Map our boards.yml board id to nextpnr-ecp5 (DEVICE, PACKAGE, SPEED).
# Mirrors the (DEVICE, PACKAGE) tuples from each board's BGM Makefile.
_BOARD_TO_TRELLIS = {
    "karnix_ecp5":     ("25k", "CABGA256", 6),
    "orangecrab_ecp5": ("25k", "CSFBGA285", 6),
    "colorlight75b":   ("25k", "CABGA256", 6),
}


def _select_part(board, configuration):
    bid = board.get("Id") or ""
    info = _BOARD_TO_TRELLIS.get(bid)
    if info is not None:
        return info
    # Fallback: parse `LFE5U[M]-<size>F[-<speed>][BG<pkg>]` style strings.
    part = board.get("Part") or ""
    m = re.match(r"^LFE5UM?-(\d+)F(?:-(\d+))?(?:([CB]G\d+))?$", part)
    if m:
        device = m.group(1) + "k"
        speed = int(m.group(2)) if m.group(2) else 6
        pkg = "CABGA{}".format(m.group(3)[2:]) if m.group(3) else "CABGA256"
        return device, pkg, speed
    return None


def synthesize(*, dir, configuration, board, board_pinmap, toolchain, peripherals,
               top, generated_top=None, include=None, output, step="full", **_):
    """Synthesize through yosys + nextpnr-ecp5 + ecppack. Returns 0 on success."""
    resolved = {
        "configuration": configuration,
        "board":         board,
        "board_pinmap":  board_pinmap,
        "toolchain":     toolchain,
        "peripherals":   peripherals,
    }

    if generated_top is None:
        generated_top = os.path.join(output, "top.sv")
        with open(generated_top, "w") as f:
            f.write(codegen.emit_top_sv(resolved))

    info = _select_part(board, configuration)
    if info is None:
        log.error("Unrecognized ECP5 board %r — extend _BOARD_TO_TRELLIS.", board.get("Id"))
        return 1
    device, package, _speed = info

    sv_files = _collect_sv_sources(REPO, peripherals, top, generated_top)
    lpf_path = os.path.join(output, PROJECT_NAME + ".lpf")
    json_path = os.path.join(output, PROJECT_NAME + ".json")
    config_path = os.path.join(output, PROJECT_NAME + ".config")
    bit_path = os.path.join(output, PROJECT_NAME + ".bit")
    yosys_log = os.path.join(output, "yosys.log")
    nextpnr_log = os.path.join(output, "nextpnr.log")

    with open(lpf_path, "w") as f:
        f.write(codegen.emit_lpf(resolved))
    log.info("Wrote %s", lpf_path)

    log.info("Source files (%d):", len(sv_files))
    for sv in sv_files:
        log.info("  - %s", os.path.relpath(sv, REPO) if sv.startswith(REPO) else sv)

    if os.environ.get("UNIFPGA_DRY_RUN"):
        log.info("[dry run] tools not invoked. Artifacts in %s", output)
        return 0

    yosys = _resolve_bin("yosys")
    if yosys is None:
        log.error("Could not find yosys on $PATH.")
        return 1

    # ---- yosys synth ----
    # `-D __ICARUS__`: BGM labs use `\`ifdef __ICARUS__` to gate older Verilog
    # syntax against SV-2009 `'{ … }` array-init that yosys still rejects.
    read_cmds = ['read_verilog -sv -D __ICARUS__ "{}"'.format(sv) for sv in sv_files]
    yosys_script = "; ".join(
        read_cmds
        + ['synth_ecp5 -top top -json "{}"'.format(json_path)]
    )
    cmd = [yosys, "-q", "-l", yosys_log, "-p", yosys_script]
    log.info("Invoking yosys synth_ecp5")
    try:
        rc = subprocess.run(cmd, cwd=output).returncode
    except FileNotFoundError as exc:
        log.error("yosys invocation failed: %s", exc)
        return 1
    if rc != 0:
        log.error("yosys exited with code %d (see %s)", rc, yosys_log)
        return rc

    if step == "elaborate":
        log.info("[elaborate] yosys synth complete; skipping nextpnr/ecppack.")
        return 0

    # ---- nextpnr place-and-route ----
    nextpnr = _resolve_bin("nextpnr-ecp5")
    if nextpnr is None:
        log.error("Could not find nextpnr-ecp5 on $PATH.")
        return 1
    cmd = [nextpnr, "--{}".format(device), "--package", package,
           "--json", json_path, "--lpf", lpf_path,
           "--textcfg", config_path, "--lpf-allow-unconstrained",
           "-q", "-l", nextpnr_log]
    log.info("Invoking nextpnr-ecp5 --%s --package %s", device, package)
    rc = subprocess.run(cmd, cwd=output).returncode
    if rc != 0:
        log.error("nextpnr-ecp5 exited with code %d (see %s)", rc, nextpnr_log)
        return rc

    # ---- ecppack bitstream ----
    ecppack = _resolve_bin("ecppack")
    if ecppack is None:
        log.error("Could not find ecppack on $PATH.")
        return 1
    rc = subprocess.run([ecppack, "--svf", bit_path + ".svf",
                         config_path, bit_path], cwd=output).returncode
    if rc != 0:
        log.error("ecppack exited with code %d", rc)
        return rc

    log.info("Bitstream ready: %s", bit_path)
    return 0


def program(*, board, board_pinmap=None, toolchain, output, **_):
    """Download the .bit to the connected board via openFPGALoader (or ecpdap)."""
    bit = os.path.join(output, PROJECT_NAME + ".bit")
    if not os.path.exists(bit) and not os.environ.get("UNIFPGA_DRY_RUN"):
        log.error("Bitstream not found: %s — run synthesis first", bit)
        return 1
    if os.environ.get("UNIFPGA_DRY_RUN"):
        log.info("[dry run] Would program %s", bit)
        return 0
    pgm = _resolve_bin("openFPGALoader") or _resolve_bin("ecpdap")
    if pgm is None:
        log.error("Could not find openFPGALoader or ecpdap on $PATH.")
        return 1
    cmd = [pgm, bit]
    log.info("Programming via: %s", " ".join(cmd))
    rc = subprocess.run(cmd, cwd=output).returncode
    if rc != 0:
        log.error("Programming failed (exit %d). Is the board connected?", rc)
    return rc
