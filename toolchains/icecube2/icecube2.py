"""
Lattice iCEcube2 toolchain driver — STUB.

iCEcube2 was the proprietary vendor flow for the iCE40 family
(LP / HX / LM / UltraPlus). It was inherited from the SiliconBlue
acquisition in 2011 and is still distributed by Lattice in maintenance
mode (last release ~2020.12). For all-iCE40 targets it's been largely
superseded by Radiant (UltraPlus only) on the proprietary side and by
the yosys + nextpnr-ice40 + Project IceStorm open flow.

Stub module. Synthesis isn't wired up yet — this just logs what would
have been built and returns 0. Replace `synthesize()` with a real driver
when implementing.
"""

import logging

log = logging.getLogger(__name__)


def synthesize(*, dir, configuration, board, board_pinmap, toolchain,
               peripherals, top, include, output, step="full", **_):
    log.info(
        "[stub %s] would synthesize configuration=%s, board=%s, top=%s, "
        "step=%s, output=%s, peripherals=%d",
        toolchain["Id"], configuration["id"], board["Id"], top, step, output,
        len(peripherals),
    )
    return 0


def program(**kwargs):
    log.info("[stub program] not implemented")
    return 0
