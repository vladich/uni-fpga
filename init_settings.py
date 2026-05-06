#!/usr/bin/env python3
"""
Interactive bootstrap: pick a default configuration (board + toolchain +
peripherals) and write settings.yml. Run once before invoking synthesize.py.
"""

import sys

import config.init


if __name__ == "__main__":
    try:
        config.init.init()
    except config.init.ConfigError as exc:
        print("Error: {e}".format(e=exc), file=sys.stderr)
        sys.exit(1)
