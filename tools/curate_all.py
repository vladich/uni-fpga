"""
Run curate_board on every harvested board (config/boards/_raw/*.yml).
Reports, per board, classified vs unclassified signal counts.
"""

import os
import sys

import yaml

from tools import curate_board


REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
RAW_DIR = os.path.join(REPO, "config", "boards", "_raw")
OUT_DIR = os.path.join(REPO, "config", "boards")


def main():
    raw_files = sorted(f for f in os.listdir(RAW_DIR) if f.endswith(".yml"))
    summary = []
    for fname in raw_files:
        board_id = fname[:-4]
        out = curate_board.curate(board_id)

        # Tally classified vs unclassified for visibility.
        with open(os.path.join(RAW_DIR, fname)) as f:
            raw = yaml.safe_load(f)
        signals = raw.get("signals") or {}
        unclassified = 0
        for sig in signals:
            if curate_board.classify(sig) is None:
                unclassified += 1
        classified = len(signals) - unclassified

        out_path = os.path.join(OUT_DIR, board_id + ".yml")
        with open(out_path, "w") as f:
            f.write(out)
        summary.append((board_id, classified, unclassified))

    print("Curated {n} boards".format(n=len(summary)))
    print("{:<32s} {:>10s} {:>14s}".format("board", "classified", "unclassified"))
    total_c = total_u = 0
    for board_id, c, u in summary:
        flag = " *" if u > 0 else ""
        print("  {:<30s} {:>10d} {:>14d}{}".format(board_id, c, u, flag))
        total_c += c
        total_u += u
    print("  {:<30s} {:>10d} {:>14d}".format("TOTAL", total_c, total_u))
    return 0


if __name__ == "__main__":
    sys.exit(main())
