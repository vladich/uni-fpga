#!/usr/bin/env python3
"""
Load a board's pin configuration YAML and expose it as a dict.

Run as a script for a quick dump:

    python -m config.xdc nexys4_ddr
"""

import os
import sys

import yaml


dir_path = os.path.dirname(os.path.realpath(__file__))


def convert_to_xdc(board_id):
    """
    Read /config/boards/<board_id>.yml and return its 'Board' dict.
    Raises FileNotFoundError if the per-board YAML is missing and yaml.YAMLError
    if the YAML is malformed or has no 'Board' root.
    """
    path = os.path.join(dir_path, "boards", "{id}.yml".format(id=board_id))
    with open(path) as board_file:
        board_yaml = yaml.safe_load(board_file)
    if board_yaml is None or "Board" not in board_yaml:
        raise yaml.YAMLError("There is no 'Board' root element in {id}.yml".format(id=board_id))
    return dict(board_yaml["Board"])


def _main(argv):
    if len(argv) != 2:
        print("usage: xdc.py <board_id>", file=sys.stderr)
        return 2
    xdc = convert_to_xdc(argv[1])
    print(xdc)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
