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
# Test logs: tests/poc/logplugin.py (in the container tests/poc_$ME/logplugin.py) writes
# reports/logs/<profile>-<time>/. The container's working directory /sai-challenger/tests is not
# on sys.path, so PYTHONPATH=/sai-challenger lets -p import it. POC_TESTLOG=0 switches it off.
PLUGIN=()
[ -f "$ROOT/tests/poc/logplugin.py" ] && PLUGIN=(-p "tests.poc_$ME.logplugin")
mkdir -p "$ROOT/tests/poc/reports"
cd /opt/sai-poc/SAI-Challenger        # exec.sh must run from here
echo "Waiting for the $TARGET lock..."
flock /opt/sai-poc/locks/$LOCK.lock "${EXEC[@]}" \
  env PYTHONDONTWRITEBYTECODE=1 POC_TARGET_PROFILE=$TARGET POC_TESTLOG=${POC_TESTLOG:-1} \
  PYTHONPATH=/sai-challenger pytest -p no:cacheprovider "${PLUGIN[@]}" \
  --testbed=$TB -v "$@" \
  --html=$OUT.html --self-contained-html --junitxml=$OUT.xml
rc=$?
"${EXEC[@]}" chown -R "$(id -u):$(id -g)" poc_$ME/reports
# poc_$ME in the container is this clone's tests/poc on the host (RUNBOOK section 4)
echo "report: tests/$OUT.{html,xml} in the container = $ROOT/tests/poc/reports/$TARGET-$STAMP.{html,xml}  exit=$rc"
echo "logs:   $ROOT/tests/poc/reports/logs/$TARGET-<MMDD-HHMMSS>/ (newest)"
exit $rc
