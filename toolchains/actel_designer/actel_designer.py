"""
Actel Designer toolchain driver — STUB.

Actel Designer is the pre-Libero Actel flow (1980s/90s). It covers the
antifuse FPGA generations (ACT 1 / 2 / 3, eX, SX / SX-A, MX, A40MX /
A42MX), and the rad-hard RTSX-S/SU series. The flow was eventually
folded into Libero IDE; some very old families have no Libero path at
all and require Designer. The tool is frozen.

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
