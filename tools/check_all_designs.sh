#!/bin/bash
# Run `synthesize.py --step elaborate` on every design against nexys4_ddr_default.
# Logs per-design pass/fail to /tmp/design_check_results.tsv.
#
# Usage:  bash tools/check_all_designs.sh

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO"
export PYTHONPATH=.
unset UNIFPGA_DRY_RUN

OUT=/tmp/design_check
mkdir -p "$OUT"
RESULTS=/tmp/design_check_results.tsv
> "$RESULTS"

count=0
for d in designs/*/; do
  design=$(basename "$d")
  [ -f "$d/design_top.sv" ] || continue
  count=$((count+1))
  out=$OUT/$design
  rm -rf "$out"
  mkdir -p "$out"
  start=$(date +%s)
  if python3 synthesize.py -c nexys4_ddr_default --top "$d/design_top.sv" -o "$out" --step elaborate \
       > "$out/synthesize.log" 2>&1; then
    rc=0
  else
    rc=$?
  fi
  elapsed=$(( $(date +%s) - start ))
  if [ "$rc" = 0 ]; then
    printf "OK\t%s\t%ds\n" "$design" "$elapsed" | tee -a "$RESULTS"
  else
    # Pull the first ERROR line from Vivado log, else from synthesize log.
    err=$(grep -m1 "^ERROR" "$out/vivado.log" 2>/dev/null || head -1 "$out/synthesize.log" 2>/dev/null | head -c 100)
    printf "FAIL(%s)\t%s\t%ds\t%s\n" "$rc" "$design" "$elapsed" "$err" | tee -a "$RESULTS"
  fi
done

echo
echo "===================="
ok=$(grep -c '^OK' "$RESULTS")
fl=$(grep -c '^FAIL' "$RESULTS")
echo "TOTAL: $count   OK: $ok   FAIL: $fl"
