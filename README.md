# uni-fpga

Multi-vendor FPGA build orchestrator. Takes one user-written `lab_top.sv` and
synthesizes it against any of 90+ board configurations across six toolchains
(Vivado, Quartus Prime, Quartus II, Gowin EDA, nextpnr/yosys-ice40,
nextpnr/yosys-ecp5, nextpnr/yosys-gowin) — without changing the lab.

## Why it exists

Every FPGA learning resource ends up in the same trap: pick one board and
write code for that board's pinout, that board's display, that board's clock,
that vendor's preferred toolchain. Move to a different board and start over.

`uni-fpga` decouples the three concerns:

1. **The lab** is plain SystemVerilog written against a fixed virtual-device
   interface (see `peripherals/lab_top_interface.sv`). It refers to abstract
   capabilities — `led`, `btn`, `sw`, `abcdefgh`/`digit`, `red`/`green`/`blue`
   pixel out, `mic_sample` — never to physical pins.
2. **The board** is described by a YAML pinmap in `config/boards/<id>.yml`
   plus a configuration in `config/configurations/<id>.yml` that "attaches"
   peripherals (`led_bank`, `vga_4bit`, `tm1638_led_key`, `inmp441_i2s_mic`,
   …) to specific pin banks.
3. **The toolchain** lives in `toolchains/<id>/<id>.py` and knows how to
   drive its vendor tool in batch mode (Vivado XDC, Quartus QSF/SDC, Gowin
   CST, iCE40 PCF, ECP5 LPF).

`synthesize.py` glues them together: it codegens a `top.sv` wrapper that maps
the board's physical pins to the lab's virtual capability ports, emits the
right constraint file, then dispatches to the toolchain driver.

A `// requires:` block at the top of any lab declares hard capability needs
(`screen >= 320x240`, `leds >= 4`, `gpio >= 8`, …) which `synthesize.py`
checks against the resolved configuration before invoking any tool. Boards
that don't meet the requirements **skip** instead of failing — surfacing the
real reason cleanly.

## Relationship to basics-graphics-music

This project is a re-architecture of, and tightly coupled to, the
[basics-graphics-music](https://github.com/yuri-panchul/basics-graphics-music)
(BGM) educational repo. Specifically:

- **Labs** in `labs/<lab_name>/` are mechanically adapted from
  `basics-graphics-music/labs/.../<lab_name>/lab_top.sv` by `tools/adapt_labs.py`.
  The adapter retargets each lab to uni-fpga's canonical port list (e.g.
  `key` → `btn`, `mic` → `mic_sample`/`mic_valid`), strips per-board includes
  that codegen replaces, infers `// requires:` blocks from access patterns,
  and applies a small per-toolchain compatibility pass (move package imports
  out of ANSI port lists, strip `<param>'(expr)` size casts, etc.).
- **Board pinmaps** in `config/boards/<id>.yml` are auto-curated from BGM's
  `boards/<id>/board_specific.{xdc,qsf,cst,pcf,lpf}` files by
  `tools/curate_board.py`. Each pin bank we name (`onboard_leds`,
  `onboard_7seg.anodes`, `pmod_jc`, `gpio_0`, …) maps directly to the rows
  of those BGM constraint files.
- **Configurations** (the per-board peripheral attachment plans) in
  `config/configurations/<id>.yml` are bootstrapped by
  `tools/generate_variants.py` from BGM directory naming conventions
  (`tang_nano_9k_lcd_480_272_no_tm1638_yosys`, `nexys4_ddr_default`, etc.).

The two repos are expected to live as **siblings** in a parent directory:

```
some-parent/
├── basics-graphics-music/   # upstream educational labs (read-only here)
└── uni-fpga/                # this repo
```

`tools/adapt_labs.py`, `tools/curate_board.py`, and
`tools/generate_variants.py` walk into `../basics-graphics-music/` directly.
You do not need to modify BGM — uni-fpga consumes it as a source of truth
and writes adapted artifacts into `labs/`, `config/boards/`, and
`config/configurations/`.

## What's in the box

| Path | What it holds |
|---|---|
| `synthesize.py` | Top-level entry point. Resolves a configuration, codegens `top.sv`, dispatches to the toolchain. |
| `program.py` | Downloads the resulting bitstream to the connected board (per-toolchain JTAG/USB programmer). |
| `config/boards/<id>.yml` | Board pinmaps (49 boards). |
| `config/boards.yml` | Board metadata (chip family, package, programmer info). |
| `config/configurations/<id>.yml` | Board × add-ons combinations (90+ configurations). |
| `config/peripherals/*.yml` | 32 peripheral definitions (`led_bank`, `vga_4bit`, `pmod_12pin`, `tm1638_led_key`, `inmp441_i2s_mic`, …). |
| `config/capabilities/*.yml` | 12 abstract user-facing capabilities (`leds`, `screen`, `gpio`, `audio_in`, …) with aggregation rules. |
| `peripherals/*.sv` | Driver SV modules for hardware peripherals (TM1638 controller, VGA, I²S mic, etc.). |
| `peripherals/labs_common/*.sv` | Reusable helpers labs reach for (`seven_segment_display`, `shift_reg`, `strobe_gen`, …). |
| `peripherals/lab_top_interface.sv` | Canonical lab_top port list — copy and add your logic. |
| `labs/<name>/lab_top.sv` | 92 adapted labs from BGM. |
| `tools/codegen.py` | Generates `top.sv` and per-toolchain constraint files from a resolved configuration. |
| `tools/adapt_labs.py` | Mechanically rewrites BGM labs into uni-fpga form. |
| `tools/curate_board.py` | Builds `config/boards/<id>.yml` from BGM constraint files. |
| `tools/generate_variants.py` | Bootstraps `config/configurations/<id>.yml` from BGM directory naming. |
| `toolchains/<id>/<id>.py` | Per-toolchain driver. Each defines `synthesize(...)` and `program(...)`. |

## Toolchain coverage

| Toolchain | Configs | Sample boards | Status |
|---|---|---|---|
| `vivado` | 10 | Nexys 4 DDR, Basys 3, Arty A7, Zybo Z7 | Validated end-to-end |
| `quartus_prime` | 24 | DE10-Lite, DE10-Nano, DE0-CV, DE2-115 | Validated end-to-end |
| `quartus2` | 8 | DE0, DE1, DE2, omdazz, marsohod | MAX II works in 23.1std; Cyclone II/III need Quartus II 13.0sp1 |
| `gowin_eda` | 37 | Tang Nano 9K/20K, Tang Primer 20K/25K | Validated end-to-end (3 high-end boards need Gowin EDA Standard license) |
| `nextpnr_icestorm` | 8 | iCEBreaker, iCE40-HX8K-EVB | Validated end-to-end (yosys 0.36 SV gaps for ~17 labs) |
| `nextpnr_trellis` | 3 | Colorlight 5A-75B, OrangeCrab, Karnix | Validated end-to-end (same yosys SV gaps) |
| `nextpnr_apicula` | 8 | Tang Nano 9K (open flow) | Validated end-to-end (same yosys SV gaps) |

## Quick start

```bash
# Pick a configuration and a lab:
PYTHONPATH=. python3 synthesize.py \
    -c basys3 \
    --top labs/2_9_pong/lab_top.sv \
    -o build/ \
    --step elaborate     # or --step full to produce a bitstream

# Program the connected board:
PYTHONPATH=. python3 synthesize.py \
    -c basys3 \
    --top labs/2_9_pong/lab_top.sv \
    -o build/ \
    --step full \
    --program
```

A configuration that doesn't meet the lab's `// requires:` block exits
with code `2` and a message naming the missing capability — that's the
intended SKIP, not a failure.

## Adding things

- **A new lab**: copy `peripherals/lab_top_interface.sv` to
  `labs/<your_lab>/lab_top.sv`, add your logic in the body, optionally add a
  `// requires:` block.
- **A new board**: drop the BGM-style constraint file under
  `basics-graphics-music/boards/<id>/` and run
  `python3 tools/curate_board.py` then `python3 tools/generate_variants.py`.
  Hand-edit the configuration's peripheral `attach:` list as needed.
- **A new toolchain**: add `toolchains/<id>/<id>.py` exposing `synthesize`
  and `program`, plus a `config/toolchains.yml` entry. The five drivers
  already in the tree are good templates — vivado for vendor TCL flows,
  nextpnr_icestorm for yosys/nextpnr open flows.
