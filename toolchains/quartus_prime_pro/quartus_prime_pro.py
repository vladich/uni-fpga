"""
Quartus Prime Pro Edition toolchain driver.

Pro is Intel/Altera's paid top-tier Quartus edition. Required for
Stratix 10, Agilex 3/5/7/9, Arria 10. Cheaper editions don't support
these families.

Same TCL/QSF/SDC flow as Lite/Standard — only InstallDir differs. Thin
re-export of the shared `toolchains.quartus_prime` implementation.
"""

from toolchains.quartus_prime.quartus_prime import synthesize, program  # noqa: F401
