#!/usr/bin/env bash
# usage: pre_pr.sh poc_<you>/test_poc_l2.py
# Validates the target profiles, runs the file twice on saivs, then upstream
# test_l2_basic.py on saivs, and writes the tail of each run to ~/pr_paste_<stamp>.txt.
set -u
T=${1:?usage: pre_pr.sh poc_<you>/test_poc_l2.py}
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=~/pr_paste_$(date +%m%d-%H%M%S).txt
for f in "$HERE"/../../tests/poc/targets/*.json; do
  python3 -m json.tool "$f" > /dev/null || { echo "invalid JSON: $f"; exit 1; }
done
fail=0
run() {  # run <label> <run_poc.sh args...>
  local label=$1; shift
  echo "== $label ==" | tee -a "$OUT"
  "$HERE/run_poc.sh" "$@" 2>&1 | tail -n 40 | tee -a "$OUT"
  [ "${PIPESTATUS[0]}" -eq 0 ] || fail=1
}
run "saivs run 1" saivs "$T" -rxX
run "saivs run 2" saivs "$T" -rxX
run "upstream test_l2_basic.py after (saivs)" saivs test_l2_basic.py -k "not lag and not mtu and not bulk"
echo "pre_pr: $([ $fail -eq 0 ] && echo PASS || echo FAIL)  -> $OUT"
exit $fail
