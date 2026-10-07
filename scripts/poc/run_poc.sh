#!/usr/bin/env bash
# usage: run_poc.sh saivs|saivpp <tests> [pytest options]
# example: run_poc.sh saivpp native/test_fdb.py --traffic
set -u
TARGET=${1:?usage: run_poc.sh saivs|saivpp <tests> [pytest options]}; shift
ME=$(whoami); STAMP=$(date +%m%d-%H%M%S)
case $TARGET in
  saivs)  EXEC=(./exec.sh --no-tty); TB=saivs_standalone ;;
  saivpp) EXEC=(./exec.sh -a vpp -t saivpp --no-tty); TB=saivpp_standalone ;;
  *) echo "unknown target: $TARGET"; exit 1 ;;
esac
OUT=poc_$ME/reports/$TARGET-$STAMP
mkdir -p ~/SAI-Challenger/tests/poc/reports
cd /opt/sai-poc/SAI-Challenger        # exec.sh must run from here
echo "Waiting for the $TARGET lock..."
flock /opt/sai-poc/locks/$TARGET.lock "${EXEC[@]}" \
  env PYTHONDONTWRITEBYTECODE=1 pytest -p no:cacheprovider \
  --testbed=$TB -v "$@" \
  --html=$OUT.html --self-contained-html --junitxml=$OUT.xml
rc=$?
"${EXEC[@]}" chown -R "$(id -u):$(id -g)" poc_$ME/reports
exit $rc
