"""
nextpnr-oxide toolchain driver.

"Oxide" is the older name for the Lattice Nexus open flow (yosys +
nextpnr-nexus + prjoxide pack). The bitstream database was originally
released as `prjnexus` then renamed to `prjoxide`, and some downstream
docs still refer to the toolchain by the `oxide` name. The actual binaries
shipped in oss-cad-suite are `nextpnr-nexus` and `prjoxide`.

This driver is therefore a thin re-export of the nextpnr_nexus module —
identical flow, identical binaries, only the toolchain id differs so a
configuration can opt into the historical name.
"""

# All synth/program logic lives in nextpnr_nexus and is reused verbatim.
from toolchains.nextpnr_nexus.nextpnr_nexus import synthesize, program  # noqa: F401
