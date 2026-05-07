#!/bin/bash
# Sweep multiple boards × multiple labs through Vivado elaborate.
# Results in /tmp/board_sweep.tsv  (board\tlab\tstatus\telapsed\terror).
#
# Usage:
#   bash tools/sweep_boards.sh <board1> <board2> ...        # all labs
#   LABS="06_binary_counter 1_01_*" bash tools/sweep_boards.sh <board>...

set -u
# Resolve repo root from the location of this script (tools/sweep_boards.sh).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO"
export PYTHONPATH=.
unset UNIFPGA_DRY_RUN

BOARDS="$@"
if [ -z "$BOARDS" ]; then
  echo "Usage: $0 <board1> <board2> ..."
  exit 2
fi

# Use $LABS env var if set, else all labs.
if [ -z "${LABS:-}" ]; then
  LABS=$(ls labs/ | grep -v '^README' | grep -v '\.md$')
fi

OUT=/tmp/board_sweep
mkdir -p "$OUT"
RESULTS=/tmp/board_sweep.tsv

for cfg in $BOARDS; do
  for lab in $LABS; do
    [ -f "labs/$lab/lab_top.sv" ] || continue
    out=$OUT/${cfg}_${lab}
    rm -rf "$out"
    mkdir -p "$out"
    start=$(date +%s)
    if python3 synthesize.py -c "$cfg" --top "labs/$lab/lab_top.sv" -o "$out" --step elaborate \
         > "$out/synthesize.log" 2>&1; then
      rc=0
    else
      rc=$?
    fi
    elapsed=$(( $(date +%s) - start ))
    if [ "$rc" = 0 ]; then
      printf "%s\t%s\tOK\t%ds\t\n" "$cfg" "$lab" "$elapsed" >> "$RESULTS"
    elif [ "$rc" = 2 ]; then
      # synthesize.py returns 2 when the lab's // requires: block doesn't
      # match the configuration's capabilities — that's a deliberate skip.
      reason=$(grep -m1 "requirements not met\|capability " "$out/synthesize.log" 2>/dev/null | head -c 200)
      printf "%s\t%s\tSKIP\t%ds\t%s\n" "$cfg" "$lab" "$elapsed" "$reason" >> "$RESULTS"
    else
      err=$(grep -m1 "^ERROR" "$out/vivado.log" 2>/dev/null || head -1 "$out/synthesize.log" 2>/dev/null | head -c 200)
      printf "%s\t%s\tFAIL(%d)\t%ds\t%s\n" "$cfg" "$lab" "$rc" "$elapsed" "$err" >> "$RESULTS"
    fi
  done
done

echo "==== summary ===="
awk -F'\t' '{
  if ($3 ~ /^OK/)        ok[$1]++
  else if ($3 ~ /^SKIP/) skip[$1]++
  else                   fail[$1]++
}
END {
  for (b in ok)   printf "  %-22s OK=%d SKIP=%d FAIL=%d\n", b, ok[b], (skip[b]+0), (fail[b]+0)
  for (b in fail) if (!(b in ok))   printf "  %-22s OK=0 SKIP=%d FAIL=%d\n", b, (skip[b]+0), fail[b]
  for (b in skip) if (!(b in ok) && !(b in fail)) printf "  %-22s OK=0 SKIP=%d FAIL=0\n", b, skip[b]
}' "$RESULTS"
