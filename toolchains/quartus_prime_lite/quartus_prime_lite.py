"""
Quartus Prime Lite Edition toolchain driver.

Lite is Intel/Altera's free Quartus edition. It covers MAX 10, Cyclone IV
(E/GX), Cyclone V (E/GX/GT/SE/SX/ST), Cyclone V SoC, Cyclone 10 LP,
Arria II GX, plus MAX V / II CPLDs. Stratix V, Arria V, Cyclone 10 GX
need Standard. Stratix 10, Agilex, Arria 10 need Pro.

The TCL/QSF/SDC flow is identical across all three editions — same
`quartus_sh` batch shell, same `--flow compile` command, same QSF
settings. This driver is therefore a thin re-export of the shared
`toolchains.quartus_prime` implementation; only the InstallDir read
from config/toolchains.yml differs between editions.
"""

# All synth/program logic lives in the shared `quartus_prime` module.
from toolchains.quartus_prime.quartus_prime import synthesize, program  # noqa: F401
