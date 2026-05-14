"""
Quartus Prime Standard Edition toolchain driver.

Standard is Intel/Altera's paid mid-tier Quartus edition. It covers
everything Lite does plus Stratix V, Arria V, Cyclone 10 GX. Stratix 10,
Agilex, Arria 10 still need Pro.

Same TCL/QSF/SDC flow as Lite/Pro — only InstallDir differs. Thin
re-export of the shared `toolchains.quartus_prime` implementation.
"""

from toolchains.quartus_prime.quartus_prime import synthesize, program  # noqa: F401
