#!/usr/bin/env bash
# usage: run_poc.sh <profile> <tests> [pytest options]
#   profile = a file name in tests/poc/targets/ without .json (saivs, saivs-thrift, saivpp)
# example: run_poc.sh saivpp poc_$(whoami) --traffic
set -u
TARGET=${1:?usage: run_poc.sh <profile> <tests> [pytest options]}; shift
ME=$(whoami); STAMP=$(date +%m%d-%H%M%S)
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PROFILE=$ROOT/tests/poc/targets/$TARGET.json
[ -f "$PROFILE" ] || { echo "unknown target profile: $TARGET (no $PROFILE)"; exit 1; }
# line 1 = testbed, line 2 = lock name, remaining lines = exec.sh options
mapfile -t P < <(python3 -c 'import json, sys
d = json.load(open(sys.argv[1]))
print(d["testbed"]); print(d["lock"])
for a in d["exec_args"]: print(a)' "$PROFILE")
[ "${#P[@]}" -ge 2 ] || { echo "cannot read $PROFILE"; exit 1; }
TB=${P[0]}; LOCK=${P[1]}; ARGS=("${P[@]:2}")
EXEC=(./exec.sh "${ARGS[@]}" --no-tty)
OUT=poc_$ME/reports/$TARGET-$STAMP
mkdir -p "$ROOT/tests/poc/reports"
# commit of the tests for the test log header (+dirty if tests/poc or scripts/poc have local changes)
GIT=$(git -C "$ROOT" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)
[ -n "$(git -C "$ROOT" status --porcelain -- tests/poc scripts/poc 2>/dev/null)" ] && GIT=$GIT+dirty
# a stale pytest-rewritten .pyc would hide passing asserts from the test log
find "$ROOT/tests/poc" -name "*-pytest-*.pyc" -delete 2>/dev/null
cd /opt/sai-poc/SAI-Challenger        # exec.sh must run from here
echo "Waiting for the $TARGET lock..."
flock /opt/sai-poc/locks/$LOCK.lock "${EXEC[@]}" \
  env PYTHONDONTWRITEBYTECODE=1 POC_TARGET_PROFILE=$TARGET POC_GIT_COMMIT=$GIT pytest -p no:cacheprovider \
  --testbed=$TB -v -o enable_assertion_pass_hook=true "$@" \
  --html=$OUT.html --self-contained-html --junitxml=$OUT.xml
rc=$?
"${EXEC[@]}" chown -R "$(id -u):$(id -g)" poc_$ME/reports
echo "report: $ROOT/tests/poc/reports/$TARGET-$STAMP.{html,xml}  exit=$rc"
# one .log per test in the run folder; the index file name drops the seconds (mmdd-hhmm)
LOGS=$ROOT/tests/poc/reports/$TARGET-$STAMP
INDEX=$LOGS/$TARGET-${STAMP:0:9}_index.log
JSONL=$ROOT/tests/poc/reports/$TARGET-$STAMP.events.jsonl
# pytest stopped early (an interrupt ends it at once) leaves only the events: render them here
[ ! -f "$INDEX" ] && [ -f "$JSONL" ] && python3 -I "$ROOT/tests/poc/lib/testlog_render.py" "$JSONL" > /dev/null
[ -f "$INDEX" ] && echo "test logs: $LOGS/"
exit $rc
