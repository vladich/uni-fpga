"""
Run import_constraints over every physical board listed in board_manifest.BOARDS
and write raw harvested YAML to config/boards/_raw/<id>.yml.

Also writes config/_manifest.yml mapping each physical board to its variant
directories (the basis for generating per-variant configurations later).
"""

import os
import sys

from tools import board_manifest
from tools import import_constraints


BGM_BOARDS = "/home/vladimir/Projects/fpga-my/basics-graphics-music/boards"
REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
RAW_DIR = os.path.join(REPO, "config", "boards", "_raw")
MANIFEST_PATH = os.path.join(REPO, "config", "_manifest.yml")


def main():
    os.makedirs(RAW_DIR, exist_ok=True)

    manifest_lines = ["# Mapping of physical boards to their variant directories", ""]
    manifest_lines.append("Boards:")

    failures = []
    successes = []

    for board_id, representative, variants in board_manifest.BOARDS:
        # Iterate every variant directory for this physical board. Same signal
        # in multiple variants confirms its pin; different signals from
        # different variants give a comprehensive board view (e.g., Tang Nano 9k
        # has separate variants for LCD vs HDMI — both pin sets belong to the
        # board). The representative is processed first so its values win on
        # any rare conflict.
        ordered_variants = [representative] + [v for v in variants if v != representative]

        merged = {}
        primary_fmt = None
        sources = []
        conflicts = []

        for variant in ordered_variants:
            vdir = os.path.join(BGM_BOARDS, variant)
            if not os.path.isdir(vdir):
                continue
            for path in import_constraints.find_all_constraint_files(vdir):
                try:
                    signals, fmt = import_constraints.parse_file(path)
                except Exception as exc:
                    failures.append("{b}: parse {p} failed: {e}".format(b=board_id, p=path, e=exc))
                    continue
                if primary_fmt is None:
                    primary_fmt = fmt
                sources.append(os.path.relpath(path, os.path.dirname(os.path.dirname(BGM_BOARDS))))
                for sig, info in signals.items():
                    if sig in merged:
                        if merged[sig].get("pin") != info.get("pin"):
                            conflicts.append((sig, merged[sig].get("pin"), info.get("pin"), path))
                    else:
                        merged[sig] = info

        if not merged:
            failures.append("{b}: nothing harvested".format(b=board_id))
            continue

        source_str = ", ".join(sorted(set(sources)))
        out = import_constraints.emit_yaml(merged, source=source_str, fmt=primary_fmt)
        out_path = os.path.join(RAW_DIR, board_id + ".yml")
        with open(out_path, "w") as f:
            f.write(out)
        successes.append((board_id, len(merged), primary_fmt, len(set(sources))))
        if conflicts:
            failures.append(
                "{b}: {n} pin conflicts across variants (first wins: {ex})"
                .format(b=board_id, n=len(conflicts), ex=conflicts[0])
            )

        manifest_lines.append("  - id: {b}".format(b=board_id))
        manifest_lines.append("    representative: {r}".format(r=representative))
        manifest_lines.append("    variants:")
        for v in variants:
            manifest_lines.append("      - {v}".format(v=v))

    with open(MANIFEST_PATH, "w") as f:
        f.write("\n".join(manifest_lines) + "\n")

    print("Harvested {n} boards into {d}".format(n=len(successes), d=RAW_DIR))
    for bid, count, fmt, nsrc in successes:
        marker = " ({n} sources)".format(n=nsrc) if nsrc > 1 else ""
        print("  {b:32s}  {fmt:4s}  {n:4d} signals{m}".format(b=bid, fmt=fmt, n=count, m=marker))
    if failures:
        print("\nFailures:")
        for f in failures:
            print("  -", f)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
