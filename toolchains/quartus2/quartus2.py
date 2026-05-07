"""
Quartus II 13.x toolchain driver.

Quartus II 13.0sp1 is the last version that supports legacy Cyclone II,
Cyclone III, and Cyclone-I parts (used by DE0/DE1/DE2/marsohod boards).
Newer Quartus Prime drops these families; Quartus II 13 keeps them.

The TCL/QSF/SDC flow is identical to Quartus Prime — same `quartus_sh`
batch shell, same `--flow compile` command, same QSF settings — so this
driver is a thin re-export of the quartus_prime module. The only thing
that differs is the InstallDir read from config/toolchains.yml.

Setup:
  1. Download Quartus II 13.0sp1 from Intel's legacy archive
     (https://www.intel.com/content/www/us/en/programmable/downloads/
     download-center.html → archive). Linux 64-bit installer.
  2. Install (typically to ~/altera/13.0sp1/quartus/).
  3. Set toolchain.InstallDir for quartus2 in config/toolchains.yml,
     e.g. `InstallDir: "/home/vladimir/altera/13.0sp1/quartus/"`.
"""

# All synth/program logic lives in quartus_prime and is reused verbatim.
from toolchains.quartus_prime.quartus_prime import synthesize, program  # noqa: F401
