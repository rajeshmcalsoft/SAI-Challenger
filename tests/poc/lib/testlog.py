"""Automatic per-test log for tests/poc: the SAI calls, asserts, packets and results of every test.

PYTEST_DONT_REWRITE: pytest must not rewrite this module. It contains no assert statements, because a
rewritten assert in this file would re-enter the pytest_assertion_pass hook.

tests/poc/conftest.py calls configure() once per run. Next to the run's --junitxml report it writes:
    <base>.events.jsonl   every event, one JSON object per line (the source of truth)
    <base>.testlog.md     the readable log (rendered by testlog_render.py at the end of the run)
    <base>.rec            the SAI calls in sairedis.rec grammar
and it adds a "Test log" block to each test in the pytest-html report.

Where the events come from (no test file needs to change):
    SAI calls   the SAI client instance: public methods (what the test asked) and the wire layer
                (`operate` or `_operate*`, whichever the client has: what the target answered). A call
                that never reaches the wire layer and returns no status is logged without a status.
    asserts     pytest_assertion_pass (run_poc.sh sets -o enable_assertion_pass_hook=true)
    packets     the PTF dataplane instance (before_send, poll) and the ptf.testutils verify_* functions
    results     pytest's setup/call/teardown reports

Recording never changes a test: wrappers return the original result and re-raise the original exception,
recorder errors are swallowed, and nothing is printed or logged (ptf sets the root logger to DEBUG).
Turn it off for one run with: -p no:poc_testlog
"""
# Every hook and wrapper catches Exception and carries on: recording must never change a test result.
# ruff: noqa: BLE001, S110
import enum
import functools
import html
import inspect
import json
import os
import platform
import sys
import threading
import time
from pathlib import Path

import pytest

from . import testlog_render

PLUGIN_NAME = "poc_testlog"
BASE_KEY = pytest.StashKey[object]()
SCHEMA = 1
MARK = "_poc_testlog"
MAX_STR = 4000
MAX_ITEMS = 512

_HERE = Path(__file__).parent
POC_DIRS = tuple({str(_HERE.parent), str(_HERE.resolve().parent)})

SAI_API = ("create", "remove", "set", "get", "get_stats", "clear_stats", "flush_fdb_entries",
           "bulk_create", "bulk_remove", "bulk_set", "bulk_get", "get_availability", "cleanup")
SAI_WIRE = ("operate", "_operate", "_operate_attributes", "_operate_stats")
PTF_CHECKS = ("verify_packet", "verify_packets", "verify_no_packet", "verify_no_other_packets",
              "verify_no_packet_any", "verify_packets_any", "verify_packet_any_port",
              "verify_any_packet_any_port", "verify_each_packet_on_each_port",
              "verify_each_packet_on_multiple_port_lists", "verify_packet_prefix",
              "count_matched_packets", "count_matched_packets_all_ports")
PKT_ARGS = ("pkt", "exp_pkt", "exp_packet", "pkts")
PORT_ARGS = ("ports", "port_id", "port", "port_list", "ports_list")
TIMEOUT_ARGS = ("timeout", "n_timeout")


# ---------------------------------------------------------------- public entry points

def configure(config, header, is_mld=None):
    """Register the plugin for this run (called from tests/poc/conftest.py pytest_configure)."""
    pm = config.pluginmanager
    config.stash[BASE_KEY] = None
    if pm.is_blocked(PLUGIN_NAME) or pm.get_plugin(PLUGIN_NAME) is not None:
        return None
    xml = getattr(config.option, "xmlpath", None)
    if not xml:
        return None                       # no --junitxml (not a run_poc.sh run): no log
    path = Path(xml)
    if not path.is_absolute():
        path = Path(config.invocation_params.dir) / path
    base = path.with_suffix("")
    hdr = dict(header or {})
    hdr.update(_environment(config))
    hdr["run"] = base.name
    rec = Recorder(base, hdr, is_mld)
    plugin = Plugin(rec)
    if pm.register(plugin, PLUGIN_NAME) is None:
        return None
    config.stash[BASE_KEY] = base
    install_sai(rec)
    install_ptf(rec)
    return plugin


def run_dir(config):
    """Folder for this run's extra files (pcaps): reports/<target>-<stamp>/, or None without a log."""
    base = config.stash.get(BASE_KEY, None)
    return Path(base) if base else None


def record(config, kind, **fields):
    """Add an event (e.g. 'pcap') from conftest code; a no-op when the log is off."""
    plugin = config.pluginmanager.get_plugin(PLUGIN_NAME)
    if plugin is not None:
        plugin.rec.emit(kind, **fields)


# ---------------------------------------------------------------- value helpers

def _cut(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[:limit] + f"…(+{len(text) - limit} chars)"


def _repr(x):
    try:
        return _cut(repr(x), 300)
    except Exception:
        return "<unrepresentable>"


def _text(x):
    if isinstance(x, (bytes, bytearray)):
        return bytes(x).decode("utf-8", "replace")
    return x if isinstance(x, str) else _repr(x)


def _j(x, depth=0):
    """A JSON-safe copy of a value (taken before the call, so later in-place changes don't leak in)."""
    try:
        if x is None or isinstance(x, (bool, int, float)):
            return x
        if isinstance(x, str):
            return _cut(x, MAX_STR)
        if isinstance(x, (bytes, bytearray)):
            return _cut(bytes(x).decode("utf-8", "replace"), MAX_STR)
        if isinstance(x, enum.Enum):
            return ("SAI_OBJECT_TYPE_" + x.name) if type(x).__name__ == "SaiObjType" else x.name
        if depth >= 6:
            return _repr(x)
        if isinstance(x, dict):
            items = list(x.items())
            out = {str(k): _j(v, depth + 1) for k, v in items[:MAX_ITEMS]}
            if len(items) > MAX_ITEMS:
                out["…"] = f"+{len(items) - MAX_ITEMS} items"
            return out
        if isinstance(x, (list, tuple, set, frozenset)):
            seq = list(x)
            out = [_j(v, depth + 1) for v in seq[:MAX_ITEMS]]
            if len(seq) > MAX_ITEMS:
                out.append(f"…(+{len(seq) - MAX_ITEMS} items)")
            return out
        if type(x).__name__ == "SaiData":         # the class can be loaded twice: match by name
            return {"_saidata": _j(getattr(x, "data", None), depth + 1)}
        return _repr(x)
    except Exception:
        return "<unrepresentable>"


def _exc(exc):
    return {"type": type(exc).__name__, "msg": _cut(str(exc), MAX_STR)}


def _bind(sig, args, kwargs):
    if sig is None:
        return {"args": list(args), **kwargs}
    try:
        return dict(sig.bind_partial(*args, **kwargs).arguments)
    except TypeError:
        return {"args": list(args), **kwargs}


def _signature(fn):
    try:
        return inspect.signature(fn)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- packets

def _mac(b):
    return ":".join(f"{x:02x}" for x in b)


def _ip4(b):
    return ".".join(str(x) for x in b)


def _ip6(b):
    try:
        import ipaddress
        return str(ipaddress.IPv6Address(bytes(b)))
    except Exception:
        return b.hex()


def _l4(proto, b):
    if proto == 6 and len(b) >= 14:
        flags = b[13]
        names = "".join(c for bit, c in ((0x02, "S"), (0x10, "A"), (0x01, "F"), (0x04, "R"), (0x08, "P"))
                        if flags & bit)
        return f"TCP {int.from_bytes(b[0:2], 'big')}->{int.from_bytes(b[2:4], 'big')} [{names}]"
    if proto == 17 and len(b) >= 4:
        return f"UDP {int.from_bytes(b[0:2], 'big')}->{int.from_bytes(b[2:4], 'big')}"
    if proto == 1 and b:
        return f"ICMP type {b[0]}"
    if proto == 58 and b:
        return f"ICMPv6 type {b[0]}"
    return f"proto {proto}"


def frame_summary(raw):
    """One line for an Ethernet frame: MACs, VLAN tags, L3 and L4 (stdlib parser)."""
    try:
        if len(raw) < 14:
            return f"short frame ({len(raw)} B)"
        parts = [f"{_mac(raw[6:12])}->{_mac(raw[0:6])}"]
        off = 12
        etype = int.from_bytes(raw[12:14], "big")
        vlans = []
        while etype in (0x8100, 0x88A8) and len(raw) >= off + 6:
            vlans.append(str(int.from_bytes(raw[off + 2:off + 4], "big") & 0x0FFF))
            off += 4
            etype = int.from_bytes(raw[off:off + 2], "big")
        parts.append(("vlan " + "/".join(vlans)) if vlans else "untagged")
        l3 = off + 2
        if etype == 0x0800 and len(raw) >= l3 + 20:
            ihl = (raw[l3] & 0x0F) * 4
            parts.append(f"IPv4 {_ip4(raw[l3 + 12:l3 + 16])}->{_ip4(raw[l3 + 16:l3 + 20])} ttl {raw[l3 + 8]}")
            parts.append(_l4(raw[l3 + 9], raw[l3 + ihl:]))
        elif etype == 0x86DD and len(raw) >= l3 + 40:
            parts.append(f"IPv6 {_ip6(raw[l3 + 8:l3 + 24])}->{_ip6(raw[l3 + 24:l3 + 40])} hlim {raw[l3 + 7]}")
            parts.append(_l4(raw[l3 + 6], raw[l3 + 40:]))
        elif etype == 0x0806:
            parts.append("ARP")
        else:
            parts.append(f"ethertype 0x{etype:04x}")
        return " ".join(parts)
    except Exception:
        return f"frame ({len(raw)} B)"


# ---------------------------------------------------------------- recorder

class Recorder:
    """Writes events to <base>.events.jsonl and keeps the current test, phase and fixture."""

    def __init__(self, base, header, is_mld=None):
        self.base = Path(base)
        self.header = header
        self.is_mld = is_mld
        self.lock = threading.RLock()
        self.file = None
        self.closed = False
        self.seq = 0
        self.test = None
        self.phase = "session"
        self.fx = []                 # stack of (fixture name, scope) being set up
        self.api = []                # open SAI API calls (wire events attach to the innermost)
        self.pkt_depth = 0
        self.checks = []             # ptf check functions running now (innermost last)
        self.npu = None
        self.alias_fp = None
        self.aliases = {}
        self.test_events = []

    @property
    def jsonl_path(self):
        return Path(str(self.base) + ".events.jsonl")

    def _open(self):
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.jsonl_path, "w", encoding="utf-8", buffering=1)  # noqa: SIM115 (open for the whole run)
        self.seq += 1
        ev = {"v": SCHEMA, "seq": self.seq, "ts": round(time.time(), 6), "k": "session", "test": None,
              "phase": "session", **self.header}
        self.file.write(json.dumps(ev, default=_repr, separators=(",", ":")) + "\n")

    def emit(self, kind, **fields):
        try:
            with self.lock:
                if self.closed:
                    return None
                if self.file is None:
                    self._open()
                self.seq += 1
                ev = {"v": SCHEMA, "seq": self.seq, "ts": round(time.time(), 6), "k": kind,
                      "test": self.test, "phase": self.phase}
                if self.fx:
                    ev["fx"], ev["fxs"] = self.fx[-1]
                ev.update(fields)
                self.file.write(json.dumps(ev, default=_repr, separators=(",", ":")) + "\n")
                if self.test is not None:
                    self.test_events.append(ev)
                return ev
        except Exception:
            return None

    def close(self):
        with self.lock:
            self.closed = True
            if self.file is not None:
                try:
                    self.file.close()
                except Exception:
                    pass

    # ---- SAI client

    def attach_client(self, client, params):
        if getattr(client, MARK, False):
            return
        try:
            setattr(client, MARK, True)
        except Exception:
            return
        for name in SAI_API:
            fn = getattr(client, name, None)
            if callable(fn):
                setattr(client, name, self._wrap_api(name, fn))
        for name in SAI_WIRE:
            fn = getattr(client, name, None)
            if callable(fn):
                setattr(client, name, self._wrap_wire(name, fn))
        self.emit("client", cls=type(client).__name__, type=(params or {}).get("type"))

    def _wrap_api(self, name, fn):
        rec = self
        sig = _signature(fn)

        @functools.wraps(fn)
        def sai_api_call(*args, **kwargs):
            __tracebackhide__ = True
            ev = rec.sai_begin(name, sig, args, kwargs)
            try:
                ret = fn(*args, **kwargs)
            except BaseException as exc:
                rec.sai_end(ev, exc=exc)
                raise
            rec.sai_end(ev, ret=ret)
            return ret

        setattr(sai_api_call, MARK, True)
        return sai_api_call

    def _wrap_wire(self, name, fn):
        rec = self
        sig = _signature(fn)

        @functools.wraps(fn)
        def sai_wire_call(*args, **kwargs):
            __tracebackhide__ = True
            w = rec.wire_begin(name, sig, args, kwargs)
            try:
                ret = fn(*args, **kwargs)
            except BaseException as exc:
                rec.wire_end(w, exc=exc)
                raise
            rec.wire_end(w, ret=ret)
            return ret

        setattr(sai_wire_call, MARK, True)
        return sai_wire_call

    def sai_begin(self, name, sig, args, kwargs):
        try:
            bound = {k: _j(v) for k, v in _bind(sig, args, kwargs).items()}
            do_assert = bound.pop("do_assert", None)
            if do_assert is None and sig is not None and "do_assert" in sig.parameters:
                do_assert = sig.parameters["do_assert"].default
            ev = {"op": name, "args": bound, "do_assert": do_assert, "wire": [], "t0": time.time(),
                  "depth": len(self.api)}
            self.api.append(ev)
            return ev
        except Exception:
            return None

    def sai_end(self, ev, ret=None, exc=None):
        if ev is None:
            return
        try:
            for i in range(len(self.api) - 1, -1, -1):
                if self.api[i] is ev:
                    del self.api[i]
                    break
            fields = {"op": ev["op"], "args": ev["args"], "do_assert": ev["do_assert"], "wire": ev["wire"],
                      "depth": ev["depth"], "ts": round(ev["t0"], 6), "dur": round(time.time() - ev["t0"], 6)}
            if exc is not None:
                fields["exc"] = _exc(exc)
            else:
                fields["ret"] = _j(ret)
            self.emit("sai", **fields)
            if not self.api:
                self.check_aliases()
        except Exception:
            pass

    def wire_begin(self, name, sig, args, kwargs):
        try:
            w = {"fn": name}
            for k, v in _bind(sig, args, kwargs).items():
                w[k] = _j(v)
            if name == "operate":
                w["key"] = w.pop("obj", None)
            w["t0"] = time.time()
            return w
        except Exception:
            return None

    def wire_end(self, w, ret=None, exc=None):
        if w is None:
            return
        try:
            w["ms"] = round((time.time() - w.pop("t0")) * 1000, 3)
            if exc is not None:
                w["exc"] = _exc(exc)
            elif w["fn"] == "operate":
                reply = [_text(x) for x in list(ret)[:3]]       # [response, data, status], left untouched
                if len(reply) == 3:
                    w["resp"], w["data"], w["status"] = reply
            elif isinstance(ret, tuple) and len(ret) == 2:
                w["status"], w["result"] = _j(ret[0]), _j(ret[1])
            else:
                w["result"] = _j(ret)
            if self.api:
                self.api[-1]["wire"].append(w)
            else:
                self.emit("wire", **w)
        except Exception:
            pass

    # ---- readable names for baseline OIDs

    def attach_testbed(self, testbed):
        try:
            npus = getattr(testbed, "npu", None) or []
            self.npu = npus[0] if len(npus) == 1 else None
            self.check_aliases()
            dps = getattr(testbed, "dataplane", None) or []
            if len(dps) == 1:
                self.attach_dataplane(dps[0])
        except Exception:
            pass

    def check_aliases(self):
        npu = self.npu
        if npu is None:
            return
        try:
            ports = list(getattr(npu, "port_oids", None) or [])
            bps = list(getattr(npu, "dot1q_bp_oids", None) or [])
            singles = [(getattr(npu, attr, None), name) for attr, name in (
                ("switch_oid", "switch"), ("dot1q_br_oid", "1q_bridge"),
                ("default_vlan_oid", "default_vlan"), ("default_vrf_oid", "default_vrf"))]
            fp = (tuple(ports), tuple(bps), tuple(o for o, _ in singles))
            if fp == self.alias_fp:
                return
            self.alias_fp = fp
            names = {}
            for oid, name in singles:
                if isinstance(oid, str) and oid not in ("", "oid:0x0"):
                    names[oid] = name
            for i, oid in enumerate(ports):
                if isinstance(oid, str):
                    names[oid] = f"port[{i}]"
            for i, oid in enumerate(bps):
                if isinstance(oid, str):
                    names[oid] = f"bp[{i}]"
            self.aliases = names
            self.emit("aliases", map=names, default_vlan_id=_j(getattr(npu, "default_vlan_id", None)))
        except Exception:
            pass

    # ---- packets

    def packet(self, data, port=None):
        d = {}
        try:
            mask = None
            if hasattr(data, "exp_pkt") and hasattr(data, "mask"):      # ptf Mask
                mask, data = data, data.exp_pkt
            raw = bytes(data)
            d = {"len": len(raw), "sum": frame_summary(raw), "hex": raw[:256].hex()}
            if mask is not None:
                d["sum"] += " (masked)"
            if self.is_mld is not None:
                try:
                    if self.is_mld(raw):
                        d["mld"] = True
                except Exception:
                    pass
        except Exception:
            d = {"sum": _repr(data)}
        if port is not None:
            d["port"] = port
        return d

    def attach_dataplane(self, dp):
        if dp is None or getattr(dp, MARK, False):
            return
        rec = self
        try:
            setattr(dp, MARK, True)
            orig_send = dp.before_send

            def before_send(pkt, device_number=0, port_number=-1):
                __tracebackhide__ = True
                try:
                    rec.emit("send", device=device_number, **rec.packet(pkt, port=port_number))
                except Exception:
                    pass
                return orig_send(pkt, device_number=device_number, port_number=port_number)

            dp.before_send = before_send
        except Exception:
            pass
        inner = getattr(dp, "dataplane", None)
        if inner is None or getattr(inner, MARK, False):
            return
        try:
            setattr(inner, MARK, True)
            orig_poll = inner.poll
            sig = _signature(orig_poll)

            def poll(*args, **kwargs):
                __tracebackhide__ = True
                result = orig_poll(*args, **kwargs)
                rec.poll_done(_bind(sig, args, kwargs), result)
                return result

            inner.poll = poll
        except Exception:
            pass

    def poll_done(self, a, result):
        try:
            ok = type(result).__name__ == "PollSuccess"
            f = {"port": a.get("port_number"), "device": a.get("device_number", 0),
                 "timeout": a.get("timeout"), "ok": ok, "within": self.checks[-1] if self.checks else None}
            if a.get("exp_pkt") is not None:
                f["exp"] = self.packet(a["exp_pkt"])
            if ok:
                f["got"] = self.packet(result.packet, port=getattr(result, "port", None))
            else:
                f["count"] = getattr(result, "packet_count", 0)
                f["recent"] = [self.packet(p) for p in list(getattr(result, "recent_packets", None) or [])[:3]]
            self.emit("poll", **f)
        except Exception:
            pass

    def wrap_check(self, name, fn):
        rec = self
        sig = _signature(fn)

        @functools.wraps(fn)
        def ptf_check(*args, **kwargs):
            __tracebackhide__ = True
            outer = rec.pkt_depth == 0
            rec.pkt_depth += 1
            rec.checks.append(name)
            info = rec.check_begin(name, sig, args, kwargs) if outer else None
            try:
                ret = fn(*args, **kwargs)
            except BaseException as exc:
                rec.pkt_depth -= 1
                rec.checks.pop()
                if outer:
                    rec.check_end(info, exc=exc)
                raise
            rec.pkt_depth -= 1
            rec.checks.pop()
            if outer:
                rec.check_end(info, ret=ret)
            return ret

        setattr(ptf_check, MARK, True)
        return ptf_check

    def check_begin(self, name, sig, args, kwargs):
        try:
            a = _bind(sig, args, kwargs)
            info = {"func": name, "t0": time.time()}
            for key in PKT_ARGS:
                if a.get(key) is not None:
                    pkt = a[key]
                    info["exp"] = self.packet(pkt[0] if isinstance(pkt, (list, tuple)) and pkt else pkt)
                    break
            for key in PORT_ARGS:
                if key in a:
                    info["ports"] = _j(a[key])
                    break
            for key in TIMEOUT_ARGS:
                if a.get(key) is not None:
                    info[key] = _j(a[key])
            return info
        except Exception:
            return None

    def check_end(self, info, ret=None, exc=None):
        if info is None:
            return
        try:
            t0 = info.pop("t0")
            info["dur"] = round(time.time() - t0, 6)
            info["ok"] = exc is None
            if exc is not None:
                info["msg"] = _cut(str(exc), 6000)
                info["exc_type"] = type(exc).__name__
            elif isinstance(ret, int) and not isinstance(ret, bool):
                info["count"] = ret
            self.emit("pktcheck", **info)
        except Exception:
            pass


# ---------------------------------------------------------------- installing the hooks

def install_sai(rec):
    """Wrap SaiClient.spawn so every client (and so switch init) is recorded from its first call."""
    try:
        import importlib
        mod = importlib.import_module("saichallenger.common.sai_client.sai_client")
        cls = mod.SaiClient
        orig = cls.spawn
    except Exception:
        return
    if getattr(orig, MARK, False):
        return

    def spawn(params):
        __tracebackhide__ = True
        client = orig(params)
        try:
            rec.attach_client(client, params if isinstance(params, dict) else {})
        except Exception:
            pass
        return client

    setattr(spawn, MARK, True)
    cls.spawn = staticmethod(spawn)


def install_ptf(rec):
    """Wrap the ptf.testutils verify_* checks before the test modules import them."""
    try:
        from ptf import testutils
    except Exception:
        return
    for name in PTF_CHECKS:
        fn = getattr(testutils, name, None)
        if callable(fn) and not getattr(fn, MARK, False):
            setattr(testutils, name, rec.wrap_check(name, fn))


# ---------------------------------------------------------------- pytest plugin

class Plugin:
    def __init__(self, rec):
        self.rec = rec
        self.md_path = None
        self.render_error = None

    def pytest_sessionstart(self, session):
        self.rec.emit("note", text="session start")

    def pytest_collection_finish(self, session):
        self.rec.emit("collected", n=len(session.items))

    def pytest_runtest_logstart(self, nodeid, location):
        self.rec.test = nodeid
        self.rec.test_events = []
        self.rec.phase = "setup"

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_setup(self, item):
        self.rec.phase = "setup"
        try:
            info = _test_info(item)
        except Exception:
            info = {"nodeid": item.nodeid, "name": getattr(item, "name", item.nodeid)}
        self.rec.emit("test", **info)
        return (yield)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(self, item):
        self.rec.phase = "call"
        return (yield)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_teardown(self, item, nextitem):
        self.rec.phase = "teardown"
        return (yield)

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        self.rec.fx.append((fixturedef.argname, fixturedef.scope))
        try:
            result = yield
        finally:
            if self.rec.fx:
                self.rec.fx.pop()
        if fixturedef.argname == "testbed_instance":
            self.rec.attach_testbed(result)
        return result

    def pytest_fixture_post_finalizer(self, fixturedef, request):
        if self.rec.phase == "teardown" and self.rec.test is not None:
            self.rec.emit("fixture_done", name=fixturedef.argname, scope=fixturedef.scope)

    def pytest_assertion_pass(self, item, lineno, orig, expl):
        self.rec.emit("assert", file=_assert_file(), line=lineno, orig=_cut(orig, 500), expl=_cut(expl, 1000))

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(self, item, call):
        report = yield
        try:
            self._report(call, report)
            if report.when == "teardown":
                self._html(report)
        except Exception:
            pass
        return report

    def pytest_runtest_logfinish(self, nodeid, location):
        try:
            events = self.rec.test_events
            test_ev = next((e for e in events if e.get("k") == "test"), {})
            reports = {e.get("when"): e for e in events if e.get("k") == "report"}
            result = testlog_render.outcome(test_ev, reports)[0]
        except Exception:
            result = None
        self.rec.emit("test_end", nodeid=nodeid, result=result)
        self.rec.test = None
        self.rec.phase = "session"
        self.rec.test_events = []

    def pytest_sessionfinish(self, session, exitstatus):
        self.rec.emit("run_end", exitstatus=int(exitstatus))
        self.rec.close()
        try:
            self.md_path, _ = testlog_render.write_outputs(self.rec.jsonl_path)
        except Exception as exc:
            self.render_error = _repr(exc)

    def pytest_terminal_summary(self, terminalreporter):
        if self.md_path:
            terminalreporter.write_line(f"test log: {self.md_path}")
        elif self.render_error:
            terminalreporter.write_line(f"test log: rendering failed ({self.render_error}); "
                                        f"events in {self.rec.jsonl_path}")

    def pytest_unconfigure(self, config):
        self.rec.close()

    # ---- helpers

    def _report(self, call, report):
        f = {"when": report.when, "outcome": report.outcome, "dur": round(report.duration, 6)}
        wasxfail = getattr(report, "wasxfail", None)
        if wasxfail is not None:
            f["wasxfail"] = wasxfail
        lr = report.longrepr
        if isinstance(lr, str):
            f["longrepr"] = _cut(lr, 4000)
        elif isinstance(lr, tuple):
            f["longrepr"] = [str(x) for x in lr]
        elif lr is not None:
            crash = getattr(lr, "reprcrash", None)
            if crash is not None:
                f["crash"] = {"path": str(crash.path), "line": crash.lineno, "msg": _cut(crash.message, 2000)}
            f["longrepr"] = _cut(report.longreprtext, 6000)
        if call.excinfo is not None:
            f["exc"] = {"type": call.excinfo.typename, "msg": _cut(str(call.excinfo.value), MAX_STR)}
            where = _where(call.excinfo)
            if where:
                f["where"] = where
        out = getattr(report, "capstdout", "")
        if out:
            f["stdout"] = _cut(out, 8000)
        self.rec.emit("report", **f)

    def _html(self, report):
        try:
            from pytest_html import extras
        except Exception:
            return
        text = testlog_render.render_test(list(self.rec.test_events))
        block = f"<details><summary>Test log</summary><pre>{html.escape(text)}</pre></details>"
        report.extras = list(getattr(report, "extras", None) or []) + [extras.html(block)]


def _test_info(item):
    info = {"nodeid": item.nodeid, "name": item.name}
    try:
        info["file"] = str(item.path)
        info["line"] = (item.location[1] or 0) + 1
    except Exception:
        pass
    fn = getattr(item, "function", None)
    info["doc"] = inspect.getdoc(fn) if fn is not None else None
    marks = []
    for m in item.iter_markers():
        if m.name not in marks and m.name not in ("parametrize", "usefixtures", "xfail", "filterwarnings"):
            marks.append(m.name)
    info["markers"] = list(reversed(marks))
    callspec = getattr(item, "callspec", None)
    if callspec is not None:
        info["params"] = {k: _j(v) for k, v in callspec.params.items()}
    xfail = item.get_closest_marker("xfail")
    if xfail is not None:
        reason = xfail.kwargs.get("reason", xfail.args[0] if xfail.args else "")
        info["xfail"] = {"reason": _j(reason), "strict": xfail.kwargs.get("strict")}
    info["fixtures"] = list(getattr(item, "fixturenames", []))[:40]
    return info


def _assert_file():
    """File of the assert that just passed: the frame above pytest's _call_assertion_pass."""
    try:
        frame = sys._getframe(1)
        for _ in range(80):
            if frame is None:
                break
            if frame.f_code.co_name == "_call_assertion_pass":
                return frame.f_back.f_code.co_filename if frame.f_back else None
            frame = frame.f_back
    except Exception:
        pass
    return None


def _where(excinfo):
    """Last traceback entry inside tests/poc (the test or helper line), not the library's assert."""
    best = None
    try:
        for entry in excinfo.traceback:
            path = str(entry.path)
            if path.startswith(POC_DIRS) and not os.path.basename(path).startswith("testlog"):
                best = entry
    except Exception:
        return None
    if best is None:
        return None
    src = ""
    try:
        src = str(best.statement).strip().splitlines()[0]
    except Exception:
        pass
    return {"file": str(best.path), "line": best.lineno + 1, "src": _cut(src, 300)}


def _environment(config):
    out = {"python": platform.python_version()}
    try:
        from importlib import metadata
        for dist, key in (("pytest", "pytest"), ("pytest-html", "pytest_html"), ("ptf", "ptf"),
                          ("scapy", "scapy")):
            try:
                out[key] = metadata.version(dist)
            except Exception:
                out[key] = None
    except Exception:
        pass
    mod = sys.modules.get("saichallenger.common") or sys.modules.get("saichallenger")
    out["saichallenger"] = os.path.dirname(getattr(mod, "__file__", "") or "") or None
    out["commit"] = os.environ.get("POC_GIT_COMMIT") or None
    for opt, key in (("--testbed", "testbed"), ("--traffic", "traffic")):
        try:
            out[key] = config.getoption(opt)
        except Exception:
            out[key] = None
    out["start"] = time.strftime("%Y-%m-%d %H:%M:%S %z")
    return out
