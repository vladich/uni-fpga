"""
Gowin EDA Standard 1.9.9.02 toolchain driver.

Educational 1.9.11.03 doesn't unlock GW5* (AroraV) parts despite
shipping their device tables — synthesis / place-and-route fail with
license errors. The Standard license enables GW5A / GW5AST / GW5AT.

The TCL/CST/SDC flow is identical to the Educational gowin_eda driver
— same `gw_sh` batch shell, same `run all` command, same CST settings —
so this driver is a thin re-export of the gowin_eda module. The only
thing that differs is the InstallDir read from config/toolchains.yml,
which makes the resolver pick `~/Gowin/1.9.9.02/IDE/bin/gw_sh` instead
of the Educational binary.

Setup (one-time):
  1. Install Gowin EDA Standard 1.9.9.02 (from Gowin's site or the same
     installer used for Educational, with a Standard-licensed download).
  2. Drop the .lic file at `~/Gowin/gowin_E_<HOST_ID>.lic`. The HOST_ID
     in the file must match one of this machine's MAC addresses.
  3. Edit `~/Gowin/1.9.9.02/IDE/bin/gwlicense.ini` so the `lic=` line
     points to the absolute path of the .lic file (the install ships
     with a remote-server placeholder that doesn't apply here).

After that, configurations targeting GW5* boards can set
`toolchain: gowin_standard` and the Educational-licensed flow can stay
as the default for GW1N* / GW2A*.
"""

# All synth/program logic lives in gowin_eda and is reused verbatim.
from toolchains.gowin_eda.gowin_eda import synthesize, program  # noqa: F401
