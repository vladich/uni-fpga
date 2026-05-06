"""
Stub toolchain module. Synthesis isn't actually wired up yet — this just
logs what would have been built and returns. Replace `synthesize()` with a
real driver (subprocess to the vendor tool, or yosys/nextpnr invocation)
when implementing the toolchain.
"""

import logging

log = logging.getLogger(__name__)


def synthesize(*, dir, configuration, board, board_pinmap, toolchain,
               peripherals, top, include, output, step="full", **_):
    """Entry point invoked by synthesize.py. Kwargs-only to keep the signature
    extensible without breaking call sites."""
    log.info(
        "[stub %s] would synthesize configuration=%s, board=%s, top=%s, "
        "step=%s, output=%s, peripherals=%d",
        toolchain["Id"], configuration["id"], board["Id"], top, step, output,
        len(peripherals),
    )
    return 0


def program(**kwargs):
    """Placeholder for board programming/loading."""
    log.info("[stub program] not implemented")
    return 0
