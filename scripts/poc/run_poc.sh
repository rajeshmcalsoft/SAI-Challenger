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
cd /opt/sai-poc/SAI-Challenger        # exec.sh must run from here
echo "Waiting for the $TARGET lock..."
flock /opt/sai-poc/locks/$LOCK.lock "${EXEC[@]}" \
  env PYTHONDONTWRITEBYTECODE=1 POC_TARGET_PROFILE=$TARGET pytest -p no:cacheprovider \
  --testbed=$TB -v "$@" \
  --html=$OUT.html --self-contained-html --junitxml=$OUT.xml
rc=$?
"${EXEC[@]}" chown -R "$(id -u):$(id -g)" poc_$ME/reports
echo "report: $ROOT/tests/poc/reports/$TARGET-$STAMP.{html,xml}  exit=$rc"
exit $rc
