"""tests/poc/logplugin.py - pytest plugin that writes the human-readable test logs.

scripts/poc/run_poc.sh loads it with  -p tests.poc_<user>.logplugin ; no test and no conftest
imports it. It covers every test of the run (Track A l2, Track B l3, Track C acl/counters,
upstream tests) and the tests need no logging code.

Output (lib/testlog.py writes it):
    tests/poc/reports/logs/<profile>-<MMDD-HHMMSS>/NNN_<test>.log, run.log, summary.txt

A test's log gets, with no help from the test: id, docstring, markers, the test bed, every
npu call with its arguments and return value and the Redis message under it (the npu
object is wrapped once per session), what the test printed (the RESULT ... lines),
the verdict of each phase, failure text, the known-gap reason, the duration.

POC_TESTLOG=0 turns it off. The hooks only observe: they never change a test result.
"""
import json
import os
from pathlib import Path

import pytest

from .lib import testlog

HERE = Path(__file__).resolve().parent
REPORTS = HERE / "reports"
TARGETS = HERE / "targets"
RAW_ERROR = pytest.StashKey[str]()   # call-phase failure text, before a conftest rewrites the report

_STATE = {"tried": False, "runlog": None, "item": None}


def _profile_name(config):
    return (config.getoption("--target-profile", None)
            or os.environ.get("POC_TARGET_PROFILE", ""))


def _runlog(config):
    """The RunLog of this session, created on first use; None when logging is off or broken."""
    if not _STATE["tried"]:
        _STATE["tried"] = True
        if testlog.enabled():
            try:
                REPORTS.mkdir(exist_ok=True)
                _STATE["runlog"] = testlog.RunLog(REPORTS, _profile_name(config) or "poc")
            except Exception as exc:                           # noqa: BLE001
                print(f"test logging disabled: {exc}")
    return _STATE["runlog"]


def _profile(config):
    """The target profile, read from targets/<name>.json, for the test-bed header only."""
    name = _profile_name(config)
    profile = {}
    if name:
        try:
            profile = json.loads((TARGETS / f"{name}.json").read_text())
        except Exception:                                      # noqa: BLE001
            profile = {}
    profile["name"] = name or "(none)"
    return profile


def _declared_gap(item):
    """The expected-gap reason a conftest stored on this test, or None.
    tests/poc/conftest.py keeps it in item.stash under its module-level StashKey GAP."""
    try:
        for plugin in item.config.pluginmanager.get_plugins():
            key = getattr(plugin, "GAP", None)
            if isinstance(key, pytest.StashKey) and key in item.stash:
                return item.stash[key]
    except Exception:                                          # noqa: BLE001
        pass
    return None


def _printed(report, kind):
    """What the test printed to `kind` (stdout or stderr) in this phase only."""
    title = f"Captured {kind} {report.when}"
    return "".join(text for name, text in report.sections if name == title)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    _STATE["item"] = item
    rl = _runlog(item.config)
    if rl is None:
        return
    rl.begin(item, _declared_gap(item))
    rl.phase("setup")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    rl = _STATE["runlog"]
    if rl is not None:
        rl.phase("call (the test body)")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    rl = _STATE["runlog"]
    if rl is not None:
        rl.phase("teardown (cleanup)")


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_runtest_makereport(item, call):
    """Innermost wrapper, so it sees the report BEFORE outer wrappers change it (a known gap
    in tests/poc/conftest.py turns a failure into a pass and drops its text). Keeps only that
    failure text; the final outcome is taken in pytest_runtest_logreport."""
    outcome = yield
    if _STATE["runlog"] is None:
        return
    try:
        rep = outcome.get_result()
        if rep.when == "call" and rep.failed:
            crash = getattr(rep.longrepr, "reprcrash", None)
            item.stash[RAW_ERROR] = getattr(crash, "message", "") or "(see traceback)"
    except Exception:                                          # noqa: BLE001
        pass


def pytest_runtest_logreport(report):
    """The final report of one phase, after every makereport wrapper."""
    rl, item = _STATE["runlog"], _STATE["item"]
    if rl is None or rl.current is None:
        return
    try:
        log = rl.current
        for kind in ("stdout", "stderr"):
            text = _printed(report, kind)
            if text.strip():
                log.raw(f"Printed by the test during {report.when} ({kind}):")
                for line in text.rstrip("\n").split("\n"):
                    log.raw("    " + line)
        note = None
        if item is not None and item.nodeid == report.nodeid and report.when == "call" and report.passed:
            gap = next((v for k, v in reversed(item.user_properties) if k == "known_gap"), None)
            if gap:
                note = {"reason": gap, "error": item.stash.get(RAW_ERROR, "(see traceback)")}
        rl.report(report, note)
    except Exception:                                          # noqa: BLE001
        pass


def pytest_runtest_logfinish(nodeid, location):
    rl = _STATE["runlog"]
    if rl is not None:
        try:
            rl.finish()
        except Exception as exc:                               # noqa: BLE001
            print(f"test logging: finish failed: {exc}")


def pytest_unconfigure(config):
    rl = _STATE["runlog"]
    if rl is not None:
        rl.close()
        print(f"\ntest logs: {rl.dir}\n")


@pytest.fixture(scope="session", autouse=True)
def poc_testlog_session(request):
    """Wrap the npu object once, so every SAI call of every test is logged.
    If npu cannot be built, the tests that use it report that error themselves."""
    rl = _runlog(request.config)
    if rl is not None:
        try:
            npu = request.getfixturevalue("npu")
            if npu is not None:
                rl.attach(npu, request.getfixturevalue("testbed_instance"), _profile(request.config))
                rl.write_header()      # the first test's log was opened before the npu existed
        except Exception as exc:                               # noqa: BLE001
            print(f"test logging: could not instrument npu: {exc}")
    yield
