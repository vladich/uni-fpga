"""
QuickLogic qorc-sdk / ql-symbiflow toolchain driver — STUB.

The qorc-sdk is QuickLogic's officially-endorsed open-source flow for
the EOS S3 SoC (Cortex-M4 + eFPGA). It wraps yosys + VPR (via the
F4PGA / SymbiFlow `ql-symbiflow` plugin) for the FPGA fabric side,
plus standard Arm GCC for the Cortex-M4 firmware. QuickLogic is the
only commercial FPGA vendor that ships an end-to-end open flow for a
current part.

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
