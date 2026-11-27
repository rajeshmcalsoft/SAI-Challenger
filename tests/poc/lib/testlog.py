"""lib/testlog.py - human-readable test logs for tests/poc (a library, nothing to run).

What you get after a run, under  tests/poc/reports/logs/<profile>-<MMDD-HHMMSS>/ :

    NNN_<test name>.log   one file per test: test bed, goal, every step, every SAI call
                          with its arguments and RETURN VALUE, expected vs actual, verdict
    run.log               all tests in order, in one file (good for grep / scrolling)
    summary.txt           one line per test: outcome, duration, checks passed/failed

How it works (tests contain no logging code and call SAI as usual):

  1. logplugin.py (loaded by run_poc.sh with -p) creates one RunLog per pytest session
     and calls attach(npu).
  2. attach() wraps, on the npu OBJECT (not the class), the SAI-Challenger calls the
     tests use: create / remove / set / get / get_stats / clear_stats / create_fdb / ...
     and, one layer lower, sai_client.operate() which is the message that really goes to
     Redis -> syncd -> libsai.  Calls nest, so the indentation in the log shows what a
     high-level call (e.g. create_vlan_member) turns into underneath.
  3. The plugin's hooks add the test id, docstring, markers, what the test printed, the
     verdict of each phase and the failure text.

Logging must never break a test: every write is wrapped, and POC_TESTLOG=0 turns the
whole thing off.
"""
import contextlib
import functools
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

WIDTH = 100
OID_RE = re.compile(r"oid:0x[0-9a-fA-F]+(?!<)(?![0-9a-fA-F])")
OP_NAMES = {"Screate": "CREATE", "Dremove": "REMOVE", "Sset": "SET", "Sget": "GET",
            "Gget": "GET", "Cclear": "CLEAR", "Bcreate": "BULK CREATE"}

# npu methods that get wrapped (only those that exist on the object are touched)
API_METHODS = [
    "create", "remove", "set", "get", "get_stats", "clear_stats",
    "create_fdb", "remove_fdb", "create_vlan_member", "remove_vlan_member",
    "get_vlan_member", "reset",
]
QUIET_METHODS = {"reset"}   # calls made inside these are not logged (hundreds of lines)

GLOSSARY = """\
How to read these logs
  oid:0x...      SAI object id (a "virtual id" that SAI-Challenger / sairedis hands out). The log adds
                 a <TYPE> tag the first time an id is created, e.g. oid:0x7000000000012<ACL_TABLE>.
  CALL / RETURN  one call from the test into SAI-Challenger (npu.create, npu.remove, ...), with the value it gave back.
  REDIS >> / <<  the raw message under that call: SAI-Challenger pushes [key, attributes, op] on the
                 ASIC_STATE queue in Redis; syncd picks it up, calls the SAI library (saivs / saivpp / thrift)
                 and writes [status, ...] back. Indented lines belong to the call above them.
  EXPECTED/ACTUAL  every check the test makes, with what it wanted and what it got. VERDICT PASS / FAIL / INFO
                 (INFO = informational check that never fails the test).
"""


def _now():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _safe_name(text):
    return re.sub(r"[^A-Za-z0-9_.\[\]-]+", "_", text).strip("_")[:120]


def _short(text, limit=600):
    text = str(text)
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more chars]"


# --------------------------------------------------------------------------
class TestLog:
    """The log of ONE test. All text goes to its own file and to the run-wide log."""

    __test__ = False   # not a pytest test class

    def __init__(self, run, nodeid, index):
        self.run, self.nodeid, self.index = run, nodeid, index
        self.path = run.dir / f"{index:03d}_{_safe_name(nodeid.split('::', 1)[-1])}.log"
        self.fh = open(self.path, "w", encoding="utf-8")
        self.depth = 0
        self.muted = 0
        self.steps = 0
        self.checks = []          # (what, expected, actual, verdict)
        self.t0 = time.monotonic()
        self.outcome = "?"
        self.gap = None

    # ---- low level ------------------------------------------------------
    def raw(self, text="", indent=0):
        pad = "  " * (self.depth + indent)
        for line in self.run.annotate(str(text)).split("\n"):
            out = f"[{_now()}] {pad}{line}".rstrip()
            try:
                self.fh.write(out + "\n")
                self.fh.flush()
                self.run.emit_run(out, self.nodeid)
            except Exception:                                  # noqa: BLE001
                pass

    def close(self):
        try:
            self.fh.close()
        except Exception:                                      # noqa: BLE001
            pass

    # ---- narration used by tests and helpers ------------------------------
    def banner(self, title, char="="):
        self.raw(char * WIDTH)
        self.raw(title)
        self.raw(char * WIDTH)

    def step(self, text):
        """A numbered step of the test itself."""
        self.steps += 1
        self.raw("")
        self.raw(f"STEP {self.steps}: {text}")

    def action(self, text):
        """A helper-level action (lib/acl.py), shown under the current step."""
        self.raw(f"ACTION: {text}", indent=1)

    def why(self, text):
        self.raw(f"WHY   : {text}", indent=1)

    def info(self, text):
        self.raw(f"INFO  : {text}", indent=1)

    @contextlib.contextmanager
    def quiet(self, what=""):
        """Hide the CALL/RETURN/REDIS lines of a noisy bulk operation; one summary line is written."""
        if what:
            self.raw(f"({what}: the individual SAI calls are not shown)", indent=1)
        self.muted += 1
        try:
            yield
        finally:
            self.muted -= 1

    def result(self, key, value):
        self.raw(f"RESULT {key} = {self.run.annotate(value)}", indent=1)

    def expect(self, what, actual=None, expected=None, ok=None, must=True, msg=None, **_):
        """Record one check: what was wanted, what happened, verdict. Raises
        AssertionError (with `msg` or a default text) when it fails and must=True."""
        if ok is None:
            ok = actual == expected
        verdict = "PASS" if ok else ("FAIL" if must else "INFO (differs)")
        self.checks.append((what, expected, actual, verdict))
        self.raw(f"CHECK #{len(self.checks)}: {what}", indent=1)
        self.raw(f"   EXPECTED : {self.run.annotate(_short(expected))}", indent=1)
        self.raw(f"   ACTUAL   : {self.run.annotate(_short(actual))}", indent=1)
        self.raw(f"   VERDICT  : {verdict}", indent=1)
        if must and not ok:
            raise AssertionError(msg or f"{what}: expected {expected!r}, got {actual!r}")
        return ok

    # ---- SAI-Challenger call tracing (used by instrument()) ----------------
    def call(self, name, orig, args, kwargs):
        if self.muted:
            return orig(*args, **kwargs)
        desc = self.run.describe_call(name, args, kwargs)
        self.raw(f"CALL   npu.{name}({desc[0]})")
        for extra in desc[1:]:
            self.raw(extra, indent=3)
        quiet = name in QUIET_METHODS
        self.depth += 1
        if quiet:
            self.muted += 1
            self.raw("(internal calls of this operation are not logged)")
        try:
            ret = orig(*args, **kwargs)
        except BaseException as exc:                           # noqa: BLE001
            if quiet:
                self.muted -= 1
            self.depth -= 1
            self.raw(f"RAISED npu.{name}: {type(exc).__name__}: {_short(self.run.annotate(exc), 400)}")
            raise
        if quiet:
            self.muted -= 1
        self.depth -= 1
        self.run.learn(name, args, ret)
        self.raw(f"RETURN npu.{name} -> {self.run.describe_return(name, ret)}")
        return ret

    def redis(self, orig, args, kwargs):
        if self.muted:
            return orig(*args, **kwargs)
        if len(args) < 3:                       # unexpected call shape: do not guess
            return orig(*args, **kwargs)
        obj, attrs, op = args[:3]
        self.raw(f"REDIS >> LPUSH ASIC_STATE_KEY_VALUE_OP_QUEUE  op={OP_NAMES.get(op, str(op)[1:].upper().replace('_', ' '))} ({op})")
        self.raw(f"key   : {self.run.annotate(obj)}", indent=3)
        self.raw(f"attrs : {self.run.annotate(_short(attrs))}", indent=3)
        try:
            status = orig(*args, **kwargs)
        except BaseException as exc:                           # noqa: BLE001
            self.raw(f"REDIS << no answer: {type(exc).__name__}: {_short(exc, 300)}")
            raise
        shown = [s.decode("utf-8", "replace") if isinstance(s, bytes) else s for s in status]
        self.raw(f"REDIS << GETRESPONSE (syncd answered) {self.run.annotate(_short(shown, 500))}")
        return status


# --------------------------------------------------------------------------
class RunLog:
    """One per pytest session: owns the directory, run.log, summary.txt, the oid names."""

    def __init__(self, root, label):
        self.stamp = datetime.now().strftime("%m%d-%H%M%S")
        self.dir = Path(root) / "logs" / f"{label or 'poc'}-{self.stamp}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.run_fh = open(self.dir / "run.log", "w", encoding="utf-8")
        self.label = label
        self.names = {}           # oid -> "ACL_TABLE" / "PORT[0]" ...
        self.current = None
        self.records = []         # (index, nodeid, outcome, seconds, checks_pass, checks_fail, file)
        self.npu = None
        self.header_text = ""
        self.counter = 0
        self.reports = {}         # nodeid -> {phase: report summary}
        self.run_fh.write(GLOSSARY + "\n")

    # ---- plumbing --------------------------------------------------------
    def emit_run(self, line, nodeid):
        self.run_fh.write(line + "\n")
        self.run_fh.flush()

    def annotate(self, value):
        """Append <TYPE> after every known oid in `value` (first time only per text)."""
        text = value if isinstance(value, str) else str(value)

        def sub(m):
            oid = m.group(0)
            name = self.names.get(oid)
            return f"{oid}<{name}>" if name else oid
        return OID_RE.sub(sub, text)

    def name_oid(self, oid, name):
        if isinstance(oid, str) and OID_RE.fullmatch(oid) and oid != "oid:0x0":
            self.names.setdefault(oid, name)

    # ---- npu instrumentation ----------------------------------------------
    def attach(self, npu, testbed=None, profile=None):
        """Wrap the npu object once and collect the test-bed description."""
        self.npu = npu
        for i, oid in enumerate(getattr(npu, "port_oids", []) or []):
            self.name_oid(oid, f"PORT[{i}]")
        for i, oid in enumerate(getattr(npu, "dot1q_bp_oids", []) or []):
            self.name_oid(oid, f"BRIDGE_PORT[{i}]")
        self.name_oid(getattr(npu, "switch_oid", None), "SWITCH")
        self.name_oid(getattr(npu, "default_vlan_oid", None), "DEFAULT_VLAN")
        self.name_oid(getattr(npu, "default_vrf_oid", None), "DEFAULT_VRF")
        self.name_oid(getattr(npu, "dot1q_br_oid", None), "BRIDGE")
        self.header_text = self.describe_testbed(npu, testbed, profile)

        if getattr(npu, "_poc_instrumented", False):
            return
        run = self
        for name in API_METHODS:
            orig = getattr(npu, name, None)
            if not callable(orig):
                continue

            def make(n, o):
                @functools.wraps(o)
                def wrapper(*a, **k):
                    log = run.current
                    return o(*a, **k) if log is None else log.call(n, o, a, k)
                return wrapper
            setattr(npu, name, make(name, orig))
        client = getattr(npu, "sai_client", None)
        op = getattr(client, "operate", None)
        if callable(op):
            def operate(*a, **k):
                log = run.current
                return op(*a, **k) if log is None else log.redis(op, a, k)
            client.operate = operate
        npu._poc_instrumented = True

    def write_header(self):
        if self.current and self.header_text:
            self.current.raw("")
            for line in self.header_text.split("\n"):
                self.current.raw(line)

    def describe_testbed(self, npu, testbed, profile):
        L = []
        L.append("TEST BED (how the switch under test is set up)")
        L.append(f"  profile        : {profile.get('name', '(none)') if profile else '(none)'}"
                 f"   (tests/poc/targets/<profile>.json)")
        tb_name = getattr(testbed, "name", None)
        L.append(f"  testbed file   : testbeds/{tb_name}.json" if tb_name else "  testbed file   : ?")
        L.append(f"  SAI target     : {getattr(npu, 'target', '?')}  (asic={getattr(npu, 'name', '?')})")
        client = getattr(npu, "sai_client", None)
        L.append(f"  SAI client     : {type(client).__name__}"
                 + ("  -> Redis ASIC_STATE queue -> syncd -> libsai" if hasattr(client, "operate") else ""))
        L.append(f"  traffic mode   : {getattr(npu, 'run_traffic', '?')}"
                 "   (True = packets are really sent with PTF; False = control plane only)")
        L.append(f"  switch oid     : {getattr(npu, 'switch_oid', '?')}")
        ports = getattr(npu, "port_oids", []) or []
        L.append(f"  switch ports   : {len(ports)}   "
                 + ", ".join(f"[{i}]={o}" for i, o in enumerate(ports[:4])) + (" ..." if len(ports) > 4 else ""))
        L.append(f"  default VLAN   : id={getattr(npu, 'default_vlan_id', '?')} oid={getattr(npu, 'default_vlan_oid', '?')}")
        if profile:
            L.append(f"  profile ports  : {profile.get('ports')}")
            L.append(f"  traffic filters: {profile.get('traffic_filters')}")
        try:
            tb_file = Path(__file__).resolve().parents[3] / "testbeds" / f"{tb_name}.json"
            cfg = json.loads(tb_file.read_text())
            for dp in cfg.get("dataplane", []):
                groups = dp.get("port_groups", [])
                shown = ", ".join(f"ptf{g.get('alias')}={g.get('name')}" for g in groups[:4])
                L.append(f"  dataplane      : {dp.get('type')} ({dp.get('mode')}), {len(groups)} ports: {shown}"
                         + (" ..." if len(groups) > 4 else ""))
        except Exception:                                      # noqa: BLE001
            pass
        return "\n".join(L)

    # ---- describing calls and results --------------------------------------
    def _pairs(self, attrs):
        """['A','x','B','y'] -> ['A = x', 'B = y'] (or None if it is not such a list)."""
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except Exception:                                  # noqa: BLE001
                return None
        if isinstance(attrs, (list, tuple)) and len(attrs) % 2 == 0 and all(isinstance(a, str) for a in attrs):
            return [f"{attrs[i]} = {attrs[i + 1]}" for i in range(0, len(attrs), 2)]
        return None

    def describe_call(self, name, args, kwargs):
        """Returns [one-line args, extra detail lines...]."""
        parts, extra = [], []
        for a in list(args) + [f"{k}={v}" for k, v in kwargs.items()]:
            if name in ("get", "get_stats", "clear_stats") and isinstance(a, (list, tuple)) \
                    and all(isinstance(x, str) for x in a):
                # a READ: the list names what to read; a second element of get() is only a placeholder
                names = [x for x in a if x != ""]
                if name == "get" and len(a) == 2:
                    extra.append(f"read  {a[0]}   (placeholder {a[1]!r} is ignored: the real value is in the answer)")
                else:
                    extra.extend(f"{'counter' if name != 'get' else 'read'}  {x}" for x in names)
                n = 1 if (name == "get" and len(a) == 2) else len(names)
                parts.append(f"<{n} counter{'s' if n != 1 else ''}>" if name != "get" else f"<{n} attribute to read>")
                continue
            pairs = self._pairs(a) if isinstance(a, (list, tuple)) else None
            if pairs is not None and pairs:
                extra.extend("attr  " + self.annotate(p) for p in pairs)
                parts.append(f"<{len(pairs)} attribute{'s' if len(pairs) != 1 else ''}>")
            else:
                text = a.name if type(a).__name__ == "SaiObjType" else None
                parts.append(self.annotate(_short(text if text else a, 200)))
        return [", ".join(parts)] + extra

    def learn(self, name, args, ret):
        """Remember the type of objects that were just created, so later lines can say <ACL_TABLE>."""
        if name != "create" or not args:
            return
        first = args[0]
        otype = getattr(first, "name", None) or (str(first).split(":")[0].replace("SAI_OBJECT_TYPE_", "")
                                                 if isinstance(first, str) else "OBJECT")
        oid = ret[1] if isinstance(ret, tuple) and len(ret) == 2 and ret[0] == "SAI_STATUS_SUCCESS" else ret
        if isinstance(oid, str):
            self.name_oid(oid, otype)

    def describe_return(self, name, ret):
        try:
            if name == "create":
                if isinstance(ret, tuple):
                    status, oid = ret
                    return self.annotate(f"status={status}, object id={oid}")
                return self.annotate(f"object id={ret}  (new object created)")
            if name in ("remove", "set", "clear_stats", "create_fdb", "remove_fdb"):
                return self.annotate(_short(ret if ret is not None else "None (success: failures raise)"))
            if name == "get_stats":
                return self.annotate(_short(ret.counters())) if hasattr(ret, "counters") else _short(ret)
            if name == "get":
                if isinstance(ret, tuple):
                    return self.annotate(_short(ret))
                if hasattr(ret, "raw"):
                    return self.annotate(f"value={_short(ret.value())}   raw={_short(ret.raw(), 300)}")
            return self.annotate(_short(ret if ret is not None else "None"))
        except Exception:                                      # noqa: BLE001
            return _short(ret)

    # ---- per-test lifecycle (driven from logplugin.py hooks) -----------------
    def begin(self, item, expected_gap=None):
        self.counter += 1
        log = TestLog(self, item.nodeid, self.counter)
        self.current = log
        log.raw("")
        log.banner(f"TEST {self.counter}: {item.nodeid}")
        doc = (getattr(item.function, "__doc__", None) or "").strip() if hasattr(item, "function") else ""
        cls_doc = (getattr(item.cls, "__doc__", None) or "").strip() if getattr(item, "cls", None) else ""
        log.raw(f"Started  : {datetime.now().isoformat(timespec='seconds')}")
        if self.counter == 1:
            log.raw("Note     : this is the first test of the session, so its duration includes the one-time "
                    "test bed initialisation (switch create, port discovery).")
        log.raw(f"Markers  : {', '.join(sorted(m.name for m in item.iter_markers() if m.name not in ('parametrize', 'usefixtures'))) or '-'}")
        if doc:
            log.raw("Purpose  : " + doc.split("\n\n")[0].replace("\n", "\n           "))
        if cls_doc:
            log.raw("Scenario : " + cls_doc.split("\n\n")[0].replace("\n", "\n           ")[:700])
        if expected_gap:
            log.raw(f"Known gap: declared in the target profile: {expected_gap}")
            log.raw("           (the target is expected to FAIL this test; that is reported as PASSED. "
                    "If it starts to pass, the test FAILS so the entry is removed.)")
        if self.header_text:
            log.raw("")
            for line in self.header_text.split("\n"):
                log.raw(line)
        return log

    def phase(self, when):
        if self.current:
            self.current.raw("")
            self.current.raw(f"---- {when.upper()} " + "-" * (WIDTH - 8 - len(when)))

    def report(self, rep, gap_note=None):
        log = self.current
        if not log:
            return
        text = f"{rep.when}: {rep.outcome.upper()}"
        if gap_note:
            text += " (KNOWN GAP: failed as the target profile predicts)"
            log.gap = gap_note
        if getattr(rep, "wasxfail", None) is not None:
            text += f" (xfail: {rep.wasxfail})"
        log.raw(f"[{text}]")
        detail = ""
        if gap_note:
            detail = f"known gap: {gap_note['reason']}\nthe test failed with: {gap_note['error']}"
        elif rep.failed:
            crash = getattr(getattr(rep, "longrepr", None), "reprcrash", None)
            if crash is not None:
                detail = f"{crash.message}\n(raised at {Path(crash.path).name}:{crash.lineno})"
            else:    # e.g. a failure outside the test body: show the END of the traceback
                text = getattr(rep, "longreprtext", "") or ""
                detail = text[-1200:]
            detail = _short(self.annotate(detail), 2500)
        elif getattr(rep, "wasxfail", None) is not None:
            # expected failure: show the exception line(s), not pytest's source dump
            errs = [ln[1:].strip() for ln in (getattr(rep, "longreprtext", "") or "").split("\n")
                    if ln.startswith("E ")]
            detail = _short(self.annotate("\n".join(errs)), 700)
        elif rep.skipped and isinstance(getattr(rep, "longrepr", None), tuple):
            detail = str(rep.longrepr[2])
        elif rep.skipped:
            detail = _short(getattr(rep, "longreprtext", ""), 500)
        if detail:
            log.raw("Details:")
            for line in detail.split("\n"):
                log.raw("    " + line)
        self.reports.setdefault(log.nodeid, {})[rep.when] = rep

    def finish(self):
        log = self.current
        if not log:
            return
        reps = self.reports.pop(log.nodeid, {})
        outcome = "PASSED"
        for when in ("setup", "call", "teardown"):
            r = reps.get(when)
            if r is None:
                continue
            if when == "call" and log.gap and r.passed:
                outcome = "PASSED (known gap)"
                break
            if r.failed:
                outcome = "FAILED" if when == "call" else f"ERROR in {when}"
                break
            if r.skipped:
                outcome = "SKIPPED"
                break
        log.outcome = outcome
        secs = time.monotonic() - log.t0
        p = sum(1 for c in log.checks if c[3] == "PASS")
        f = sum(1 for c in log.checks if c[3] == "FAIL")
        i = sum(1 for c in log.checks if c[3].startswith("INFO"))
        log.depth = 0
        log.raw("")
        log.banner(f"SCORECARD  {log.nodeid}", "-")
        if log.checks:
            for n, (what, exp, act, verdict) in enumerate(log.checks, 1):
                log.raw(f"  #{n:<2} {verdict:<13} {what}")
                if verdict != "PASS":
                    log.raw(f"        expected: {self.annotate(_short(exp, 200))}")
                    log.raw(f"        actual  : {self.annotate(_short(act, 200))}")
        else:
            log.raw("  (this test records no explicit checks; see the outcome below)")
        log.raw(f"  CHECKS : {p} passed, {f} failed, {i} informational")
        log.raw(f"  OUTCOME: {outcome}    duration {secs:.2f}s")
        log.raw("=" * WIDTH)
        log.close()
        self.records.append((log.index, log.nodeid, outcome, secs, p, f, log.path.name))
        self.current = None

    def close(self):
        lines = [f"POC TEST RUN SUMMARY  ({self.label}, {self.stamp})", "",
                 f"{'#':>3}  {'OUTCOME':<24} {'TIME':>7}  {'CHECKS':<10} TEST / LOG FILE"]
        for idx, node, outcome, secs, p, f, fname in self.records:
            lines.append(f"{idx:>3}  {outcome:<24} {secs:6.2f}s  {p:>3}ok {f:>2}bad  {node}")
            lines.append(f"{'':>50}-> {fname}")
        counts = {}
        for r in self.records:
            key = r[2].split(" ")[0]
            counts[key] = counts.get(key, 0) + 1
        lines += ["", "TOTALS: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))]
        text = "\n".join(lines) + "\n"
        try:
            (self.dir / "summary.txt").write_text(text)
            self.run_fh.write("\n" + text)
            self.run_fh.close()
        except Exception:                                      # noqa: BLE001
            pass
        return text


def enabled():
    return os.environ.get("POC_TESTLOG", "1") not in ("0", "false", "off", "no")
