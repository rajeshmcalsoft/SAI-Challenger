"""Render a PoC test log (<run>.events.jsonl) as Markdown and as a sairedis.rec-style file.

PYTEST_DONT_REWRITE: pytest must not rewrite this module (it contains no assert statements).

Standard library only and no saichallenger import, so it also runs on the host, for example
after an interrupted run:

    python3 -I tests/poc/lib/testlog_render.py tests/poc/reports/<run>.events.jsonl

writes <run>.testlog.md and <run>.rec next to the .jsonl. tests/poc/lib/testlog.py writes the events.
"""
import itertools
import json
import re
import sys
import time
from pathlib import Path

OK = "SAI_STATUS_SUCCESS"
PROBE = "SAI_STATUS_BUFFER_OVERFLOW"
SESSION_SCOPES = ("session", "package")
OID_RE = re.compile(r"oid:0x[0-9a-fA-F]+")
ATTR_TYPE_RE = re.compile(r"SAI_([A-Z0-9_]+?)_ATTR_")
MAX_VALUE = 160
MAX_LINE_LIST = 12

RESULT_ORDER = ("Interrupted", "Fail", "Setup error", "Cleanup error", "Gap now passes", "Expected gap", "Skipped", "Pass")


# ---------------------------------------------------------------- loading

def load(path):
    """Events sorted by seq. A truncated last line (interrupted run) is skipped."""
    events = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    events.sort(key=lambda e: e.get("seq", 0))
    return events


# ---------------------------------------------------------------- small helpers

def _pairs(seq):
    """[k, v, k, v] (or its JSON text) -> [(k, v)]."""
    if isinstance(seq, str):
        try:
            seq = json.loads(seq)
        except ValueError:
            return []
    if isinstance(seq, dict):
        return [(str(k), v) for k, v in seq.items()]
    if not isinstance(seq, list):
        return []
    return [(str(seq[i]), seq[i + 1]) for i in range(0, len(seq) - 1, 2)]


def _names(seq):
    return [k for k, _ in _pairs(seq)]


def _short(text, limit=MAX_VALUE):
    text = str(text)
    return text if len(text) <= limit else text[:limit] + f"…(+{len(text) - limit})"


def _first_line(text, limit=200):
    text = str(text or "").strip()
    return _short(text.splitlines()[0] if text else "", limit)


def _type_of_key(text):
    """'SAI_OBJECT_TYPE_VLAN:oid:0x..' -> ('VLAN', 'oid:0x..'); 'oid:0x..' -> (None, 'oid:0x..')."""
    if not isinstance(text, str):
        return None, None
    if text.startswith("SAI_OBJECT_TYPE_"):
        t, _, rest = text.partition(":")
        return t[len("SAI_OBJECT_TYPE_"):], (rest or None)
    return None, text


def _ts(ev):
    return float(ev.get("ts") or 0.0)


def _rec_time(ts):
    lt = time.localtime(ts)
    return time.strftime("%Y-%m-%d.%H:%M:%S.", lt) + f"{round((ts % 1) * 1e6) % 1000000:06d}"


def _status_short(status):
    return status if status else "?"


def _rel(path):
    """Show a path from tests/poc_<user>/ onwards (container) or tests/poc/ (host)."""
    path = str(path or "")
    m = re.search(r"(tests/poc[^/]*/.*)$", path)
    return m.group(1) if m else path


# ---------------------------------------------------------------- SAI events

class Names:
    """OID -> readable name: baseline aliases from the recorder plus TYPE#n for objects a test creates."""

    def __init__(self, aliases=None):
        self.base = dict(aliases or {})
        self.local = {}
        self.types = {}       # oid -> object type (from creates and alias kinds)
        self.counts = {}
        self.shown = set()

    def set_base(self, aliases):
        self.base = merge_aliases(self.base, aliases)

    def created(self, typ, oid, baseline=False):
        if not oid or not isinstance(oid, str) or not oid.startswith("oid:"):
            return None
        if oid in self.base:
            self.types[oid] = typ
            return self.base[oid]
        self.counts[typ] = self.counts.get(typ, 0) + 1
        name = f"{typ or 'OBJ'}#{self.counts[typ]}"
        self.local[oid] = name
        self.types[oid] = typ
        return name

    def name(self, oid, first_use=False):
        name = self.local.get(oid) or self.base.get(oid)
        if not name:
            return oid
        if first_use and oid in self.local and oid not in self.shown:
            self.shown.add(oid)
            return f"{name}({oid})"
        return name

    def value(self, value):
        """Format a SAI value, replacing known OIDs by their names."""
        if isinstance(value, (list, dict)):
            value = json.dumps(value, separators=(",", ":"))
        text = OID_RE.sub(lambda m: self.local.get(m.group(0)) or self.base.get(m.group(0)) or m.group(0),
                          str(value))
        return _short(text)


def sai_view(ev):
    """Normalise one 'sai' event into display fields (works for both wire-event shapes)."""
    args = ev.get("args") or {}
    op = ev.get("op") or "?"
    wire = [w for w in (ev.get("wire") or []) if isinstance(w, dict)]
    last = wire[-1] if wire else {}
    view = {"op": op, "ev": ev, "wire": wire, "exc": ev.get("exc"), "do_assert": ev.get("do_assert")}

    # object type and id/key: redis wire key is complete; otherwise the API argument
    typ, ident = _type_of_key(last.get("key")) if isinstance(last.get("key"), str) else (None, None)
    if typ is None:
        obj = args.get("obj", args.get("obj_type"))
        typ, ident = _type_of_key(obj)
        if typ is None and isinstance(last.get("obj_type"), str):
            typ = last["obj_type"].replace("SAI_OBJECT_TYPE_", "")
        if ident is None and isinstance(last.get("oid"), (str, int)) and op != "create":
            ident = last["oid"] if isinstance(last["oid"], str) else hex(last["oid"])

    ret = ev.get("ret")
    status = last.get("status") if isinstance(last.get("status"), str) else None
    if status is None and isinstance(ret, list) and ret and isinstance(ret[0], str) and ret[0].startswith("SAI_STATUS"):
        status = ret[0]
    if status is None and isinstance(ret, str) and ret.startswith("SAI_STATUS"):
        status = ret
    if status is None and not view["exc"] and op not in ("cleanup",):
        status = OK if wire or ret is not None else None
    view["status"] = status

    # created id: an `operate` wire key carries the new VID; otherwise the call returns it
    created = None
    if op == "create":
        if isinstance(ident, str) and ident.startswith("oid:"):
            created = ident
        else:
            r = ret[1] if isinstance(ret, list) and len(ret) == 2 and isinstance(ret[0], str) else ret
            if isinstance(r, str) and r.startswith("oid:"):
                created = r
                ident = r
            elif isinstance(r, dict) and ident is None:
                ident = json.dumps(r, separators=(",", ":"))
    view["typ"], view["ident"], view["created"] = typ, ident, created

    # attributes sent and values read back
    attrs = args.get("attrs", args.get("attr"))
    view["attrs"] = _pairs(attrs) if attrs is not None else []
    if view["typ"] is None and view["attrs"] and op not in ("flush_fdb_entries", "cleanup"):
        # SAI names every attribute SAI_<OBJECT_TYPE>_ATTR_<NAME>
        m = ATTR_TYPE_RE.match(str(view["attrs"][0][0]))
        view["typ"] = m.group(1) if m else None
    data = ret[1] if isinstance(ret, list) and len(ret) == 2 and isinstance(ret[0], str) else ret
    if isinstance(data, dict) and "_saidata" in data:
        view["read"] = _pairs(data["_saidata"])
    elif op in ("get", "get_stats") and isinstance(last.get("data"), str):
        view["read"] = _pairs(last["data"])
    else:
        view["read"] = []
    return view


def sai_line(view, names, baseline=False):
    """One readable line for a SAI call (the caller adds the time offset)."""
    op, typ = view["op"], view["typ"]
    status = view["status"]
    if op == "cleanup":
        return "switch reset (SAI client cleanup: target state flushed)"
    ident = view["ident"]
    if isinstance(ident, str) and ident.startswith("oid:"):
        # a type seen earlier for the same OID (some clients send only the OID)
        if typ:
            names.types.setdefault(ident, typ)
        else:
            typ = view["typ"] = names.types.get(ident)
    if op == "create" and view["created"] and status == OK:
        names.created(typ, view["created"])
        target = names.name(view["created"], first_use=True)
    elif isinstance(ident, str) and ident.startswith("oid:"):
        known = names.local.get(ident) or names.base.get(ident)
        target = known or (f"{typ} {ident}" if typ else ident)
    elif ident:
        target = f"{typ or ''} {names.value(ident)}".strip()
    else:
        target = typ or "?"
    if op == "create" and not (view["created"] and status == OK):
        target = f"{typ or ''} {names.value(ident) if ident and not str(ident).startswith('oid:') else ''}".strip()

    parts = [op, target]
    if op in ("create", "set", "flush_fdb_entries"):
        parts += [f"{k}={names.value(v)}" for k, v in view["attrs"]]
    elif op in ("get", "get_stats", "clear_stats", "get_availability") and not (
            view["read"] and status in (OK, None)):
        attr_names = [k for k, _ in view["attrs"]]
        if len(attr_names) > MAX_LINE_LIST:
            attr_names = attr_names[:MAX_LINE_LIST] + [f"…(+{len(view['attrs']) - MAX_LINE_LIST})"]
        parts += attr_names
    elif op.startswith("bulk_"):
        keys = view["ev"].get("args", {}).get("keys")
        parts.append(f"({len(keys) if isinstance(keys, list) else '?'} objects)")
    line = " ".join(str(p) for p in parts if p != "")
    if status:
        line += f" -> {status}"
    elif op not in ("cleanup",) and not view["exc"]:
        line += " -> (no status returned by the client)"
    if view["read"] and status in (OK, None):
        shown = view["read"][:MAX_LINE_LIST]
        line += " : " + ", ".join(f"{k}={names.value(v)}" for k, v in shown)
        if len(view["read"]) > MAX_LINE_LIST:
            line += f", …(+{len(view['read']) - MAX_LINE_LIST})"
    exc = view["exc"]
    if exc:
        line += f"  !! raised {exc.get('type')}: {_first_line(exc.get('msg'), 160)}"
    return line


def merge_aliases(old, new):
    """Alias map after an 'aliases' event. A switch re-init reports partial maps while the lists
    fill in, so known names are kept; a name that now points to another OID drops the old one."""
    new = dict(new or {})
    moved = set(new.values())
    merged = {oid: name for oid, name in (old or {}).items() if name not in moved}
    merged.update(new)
    return merged


def _sig(view):
    """Grouping signature for repeated session calls (ignores which object)."""
    return (view["op"], view["typ"], tuple(k for k, _ in view["attrs"]), view["status"], bool(view["exc"]))


def _probes(views):
    """Size probes: a BUFFER_OVERFLOW get followed by the same get. Returns the probe keys and
    marks the real get with 'after_probe'. views: [(key, view)] in call order."""
    skip = set()
    for (a, v), (_, nxt) in itertools.pairwise(views):
        if v["op"] == "get" and v["status"] == PROBE and nxt["op"] == "get" and v["ident"] == nxt["ident"] \
                and [k for k, _ in v["attrs"]] == [k for k, _ in nxt["attrs"]]:
            skip.add(a)
            nxt["after_probe"] = True
    return skip


def _view_type(view, names):
    ident = view["ident"]
    return view["typ"] or (names.types.get(ident) if isinstance(ident, str) else None) \
        or _alias_type(names, ident) or "?"


def _attr_short(name, typ):
    return name.removeprefix(f"SAI_{typ}_ATTR_")


MAX_CYCLE = 12


def _collapse(items, names, indent="  "):
    """Lines for SAI calls [(offset, view, text)], in order; each call stays in the .rec.
    - A run of more than 3 successful gets becomes one line with a count per type and attribute.
    - A block of calls repeated 3 or more times with the same ops, attributes and statuses (only
      the objects differ) shows its first and last rounds and the number of rounds between."""
    units, run = [], []      # unit: (shape, lines)

    def get_ok(v):
        return v["op"] == "get" and v["status"] == OK and not v["exc"]

    def flush():
        if len(run) <= 3:
            units.extend(((v["op"], v["typ"], tuple(k for k, _ in v["attrs"]), v["status"]),
                          [f"{indent}{off} {text}"]) for off, v, text in run)
        else:
            counts = {}
            for _, v, _ in run:
                typ = _view_type(v, names)
                key = f"{typ} " + ",".join(_attr_short(k, typ) for k, _ in v["attrs"])
                counts[key] = counts.get(key, 0) + 1
            block = [f"{indent}{run[0][0]} get ×{len(run)}, all {OK} (grouped; each call is in the .rec):"]
            block.extend(f"{indent}{'':9}   {key} ×{n}" for key, n in counts.items())
            units.append((("gets",), block))
        run.clear()

    for off, v, text in items:
        if get_ok(v):
            run.append((off, v, text))
            continue
        flush()
        units.append(((v["op"], v["typ"], tuple(k for k, _ in v["attrs"]), v["status"], bool(v["exc"])),
                      [f"{indent}{off} {text}"]))
    flush()

    shapes = [s for s, _ in units]
    lines, i = [], 0
    while i < len(units):
        best = None          # (units covered, cycle length, rounds)
        for size in range(1, MAX_CYCLE + 1):
            rounds = 1
            while shapes[i + rounds * size:i + (rounds + 1) * size] == shapes[i:i + size]:
                rounds += 1
            if rounds >= 3 and (best is None or rounds * size > best[0]):
                best = (rounds * size, size, rounds)
        if best is None:
            lines.extend(units[i][1])
            i += 1
            continue
        _, size, rounds = best
        for u in units[i:i + size]:
            lines.extend(u[1])
        if size == 1:
            lines.append(f"{indent}{'':9} … {rounds - 2} more calls like this (same op, attributes and status; "
                         "other objects) …")
        else:
            lines.append(f"{indent}{'':9} … {rounds - 2} more rounds of these {size} calls (same ops, "
                         "attributes and statuses; other objects) …")
        for u in units[i + (rounds - 1) * size:i + rounds * size]:
            lines.extend(u[1])
        i += rounds * size
    return lines


# ---------------------------------------------------------------- packets

def _ports(ports):
    if not ports:
        return "-"
    if not isinstance(ports, list):
        return str(ports)
    out = []
    for p in ports:
        out.append(str(p[1]) if isinstance(p, list) and len(p) == 2 else str(p))
    return ",".join(out)


def _pkt(p):
    if not isinstance(p, dict):
        return str(p)
    tag = " [MLD]" if p.get("mld") else ""
    return f"{p.get('len', '?')}B {p.get('sum', '?')}{tag}"


def _hexdump(hx, limit=256):
    if not hx:
        return []
    raw = hx[: limit * 2]
    lines = []
    for off in range(0, len(raw), 32):
        chunk = raw[off:off + 32]
        lines.append(f"      {off // 2:04x}  " + " ".join(chunk[i:i + 2] for i in range(0, len(chunk), 2)))
    return lines


# ---------------------------------------------------------------- outcomes

def outcome(test_ev, reports):
    """(result word, detail) in the UI vocabulary from the setup/call/teardown report events."""
    setup, call, teardown = reports.get("setup"), reports.get("call"), reports.get("teardown")
    marker = (test_ev or {}).get("xfail") or {}

    def gap(rep):
        if rep.get("when") != "call":
            return "Expected gap", f"{rep.get('when')} failed, counted as expected gap by pytest"
        if not marker:
            return "Expected gap", "imperative xfail (pytest.xfail called by the code, no profile gap)"
        return "Expected gap", ""

    result, detail = "Pass", ""
    if setup and setup.get("outcome") == "failed":
        result, detail = "Setup error", ""
    elif setup and setup.get("outcome") == "skipped":
        result, detail = gap(setup) if setup.get("wasxfail") is not None else ("Skipped", "")
    elif call:
        lr = call.get("longrepr")
        if call.get("outcome") == "failed" and isinstance(lr, str) and lr.startswith("[XPASS(strict)]"):
            result = "Gap now passes"
        elif call.get("outcome") == "failed":
            result = "Fail"
        elif call.get("outcome") == "skipped":
            result, detail = gap(call) if call.get("wasxfail") is not None else ("Skipped", "")
        elif call.get("wasxfail") is not None:
            result = "Gap now passes"
    if teardown and teardown.get("outcome") == "failed":
        if result in ("Pass", "Skipped"):
            result, detail = "Cleanup error", ""
        else:
            detail = (detail + "; " if detail else "") + "cleanup also failed"
    elif teardown and teardown.get("outcome") == "skipped" and teardown.get("wasxfail") is not None \
            and result == "Pass":
        result, detail = "Expected gap", "teardown failed, counted as expected gap by pytest"
    return result, detail


def _skip_reason(rep):
    lr = rep.get("longrepr")
    if isinstance(lr, list) and len(lr) == 3:
        return str(lr[2]).replace("Skipped: ", "", 1)
    return _first_line(lr)


# ---------------------------------------------------------------- one test

def _split_test(events):
    """Separate a test's events into its own steps and session-fixture work (init, final teardown)."""
    own, session, buffer = [], [], []
    for ev in events:
        phase, kind = ev.get("phase"), ev.get("k")
        if phase == "setup" and ev.get("fxs") in SESSION_SCOPES:
            session.append(ev)
        elif phase == "teardown" and kind == "fixture_done":
            # events since the previous marker belong to the fixture that just finished
            (session if ev.get("scope") in SESSION_SCOPES else own).extend(buffer)
            buffer = []
        elif phase == "teardown" and kind in ("sai", "wire", "send", "poll", "aliases", "note"):
            buffer.append(ev)
        else:
            own.append(ev)
    own.extend(buffer)
    own.sort(key=lambda e: e.get("seq", 0))
    return own, session


def analyse_test(events, aliases):
    """Everything the Markdown section and the index need for one test."""
    test_ev = next((e for e in events if e.get("k") == "test"), {}) or {}
    reports = {}
    for e in events:
        if e.get("k") == "report":
            reports[e.get("when")] = e
    own, session = _split_test(events)
    result, detail = outcome(test_ev, reports)
    if not any(e.get("k") == "test_end" for e in events):
        # the run stopped inside this test (Ctrl-C ends pytest at once: the PTF dataplane restores
        # the default SIGINT action), so the result pytest would have given is unknown
        done = [f"{w} {reports[w].get('outcome')}" for w in ("setup", "call", "teardown") if w in reports]
        result = "Interrupted"
        detail = "the run stopped before this test finished" + (f" (done: {', '.join(done)})" if done else "")
    duration = sum(float(r.get("dur") or 0) for r in reports.values())
    return {"test": test_ev, "reports": reports, "own": own, "session": session,
            "result": result, "detail": detail, "duration": duration, "aliases": aliases}


def _failing_report(info):
    for when in ("setup", "call", "teardown"):
        rep = info["reports"].get(when)
        if rep and (rep.get("outcome") == "failed" or rep.get("wasxfail") is not None) \
                and (rep.get("exc") or rep.get("longrepr")):
            return rep
    return None


def render_test(events, aliases=None, number=None, anchor=None):
    """Markdown section for one test (also used, as plain text, in the pytest-html report)."""
    info = analyse_test(events, aliases)
    return _render_info(info, number=number, anchor=anchor)[0]


def _render_info(info, number=None, anchor=None):
    t = info["test"]
    names = Names(info["aliases"])
    out = []
    title = t.get("name") or t.get("nodeid") or "?"
    head = f"### {number}. {title}" if number else f"### {title}"
    if anchor:
        out.append(f'<a id="{anchor}"></a>')
    out.append(f"{head} · {info['result']}")
    out.append("")
    doc = (t.get("doc") or "").strip()
    first = doc.splitlines()[0] if doc else ""
    out.append(f"**{first}**" if first else "_(no docstring)_")
    out.append("")
    meta = []
    if t.get("markers"):
        meta.append(" ".join(f"`{m}`" for m in t["markers"]))
    if t.get("params"):
        meta.append("params " + ", ".join(f"`{k}={v}`" for k, v in t["params"].items()))
    meta.append(f"{info['duration']:.2f} s")
    if t.get("file"):
        meta.append(f"`{_rel(t['file'])}:{t.get('line')}`")
    out.append(" · ".join(meta))
    rest = "\n".join(doc.splitlines()[1:]).strip()
    if rest:
        out.append("")
        out.append("<details><summary>Intent (full docstring)</summary>\n\n```text\n" + rest + "\n```\n</details>")
    out.append("")

    # ---- steps first (builds names), rendered later
    steps, checks, stats = _steps(info, names)
    info["n_checks"] = sum(c.get("n", 1) for c in checks)
    info["n_sai"] = stats["sai"]

    # ---- verdict
    out.append("**Verdict:** " + _verdict(info, checks, stats))
    rep = _failing_report(info)
    if rep is not None and info["result"] not in ("Pass", "Skipped"):
        where = rep.get("where") or {}
        exc = rep.get("exc") or {}
        if where:
            out.append(f"- where: `{_rel(where.get('file'))}:{where.get('line')}`"
                       + (f"  `{_short(where.get('src'), 120)}`" if where.get("src") else ""))
        if exc.get("msg"):
            msg_lines = [ln for ln in str(exc["msg"]).splitlines() if ln.strip()][:6]
            out.append(f"- got: `{exc.get('type')}`")
            out.append("  ```text")
            out.extend("  " + _short(ln, 200) for ln in msg_lines)
            out.append("  ```")
        elif isinstance(rep.get("longrepr"), str):
            out.append(f"- got: `{_first_line(rep['longrepr'])}`")
        last_bad = stats.get("last_bad_sai")
        if last_bad:
            out.append(f"- last SAI call that did not succeed: `{last_bad}`")
        miss = stats.get("pkt_miss")
        if miss:
            seen = "; ".join(f"`{_pkt(p)}`" for p in miss["recent"]) or "nothing"
            out.append(f"- on {miss['where']} within {miss['timeout']} s, instead of the expected frame: {seen}")
    xf = t.get("xfail") or {}
    if info["result"] in ("Expected gap", "Gap now passes") and xf.get("reason"):
        out.append(f"- profile gap: \"{xf['reason']}\" (strict={xf.get('strict')})")
    if info["detail"] and info["result"] != "Interrupted":
        out.append(f"- note: {info['detail']}")
    out.append("")

    # ---- checks
    out.append("**Checks**")
    out.append("")
    if checks:
        out.append("| # | Result | Where | Check | Values |")
        out.append("|---|---|---|---|---|")
        for i, c in enumerate(checks, 1):
            out.append(f"| {i} | {c['result']} | {c['where']} | {c['check']} | {c['values']} |")
    else:
        out.append("_No assert or packet check ran._")
    out.append("")

    # ---- steps
    out.append("**Steps**")
    out.append("")
    out.append("```text")
    out.extend(steps or ["(no events)"])
    out.append("```")
    out.append("")

    # ---- cleanup, surface, evidence
    out.append("**Cleanup:** " + _cleanup_text(stats, names))
    if stats["surface"]:
        surf = []
        for typ, ops in sorted(stats["surface"].items()):
            op_text = ", ".join(f"{op}×{n}" for op, n in ops["ops"].items())
            attrs = ", ".join(sorted(ops["attrs"]))
            surf.append(f"{typ}: {op_text}" + (f" ({attrs})" if attrs else ""))
        out.append("**SAI surface:** " + "; ".join(surf) + ".")
    ev_lines = []
    for p in stats["pcaps"]:
        ev_lines.append(f"pcap `{_rel(p.get('path'))}` ({p.get('frames')} frames)")
    for when in ("setup", "call", "teardown"):
        r = info["reports"].get(when)
        if r and r.get("stdout"):
            ev_lines.append(f"{when} stdout: see below")
    if ev_lines:
        out.append("**Evidence:** " + "; ".join(ev_lines) + ".")
    for when in ("setup", "call", "teardown"):
        r = info["reports"].get(when)
        if r and r.get("stdout"):
            out.append("")
            out.append(f"<details><summary>{when} stdout</summary>\n\n```text\n{r['stdout'].rstrip()}\n```\n</details>")
    out.append("")
    return "\n".join(out), info


def _cleanup_text(stats, names):
    """Objects created and removed by the test. A pre-existing object the test removed (for example
    a default bridge port) is paired with a new object of the same type the test left in place."""
    created, removed, types, shown = stats["created"], stats["removed"], stats["types"], stats["names"]
    left = sorted(created - removed, key=lambda o: shown.get(o, o))
    pre = sorted(removed - created, key=lambda o: shown.get(o, o))
    replaced, gone = [], []
    for old in pre:
        new = next((o for o in left if types.get(o) == types.get(old)), None)
        if new is None:
            gone.append(shown.get(old, old))
        else:
            left.remove(new)
            replaced.append(f"{shown.get(old, old)} by {shown.get(new, new)}")
    parts = [f"{len(created)} created" + (f", {len(removed & created)} of them removed" if created else "")]
    if replaced:
        parts.append("objects that existed before this test, removed and replaced by a new one of the same type: "
                     + ", ".join(replaced))
    if gone:
        parts.append("objects that existed before this test, removed: " + ", ".join(gone))
    parts.append(f"{len(left)} left behind" + (f" ({', '.join(shown.get(o, o) for o in left)})" if left else ""))
    return "; ".join(parts) + "."


def _okey(ident):
    """One spelling for an object: an OID, or an entry key as sorted compact JSON."""
    if isinstance(ident, str) and ident.startswith("{"):
        try:
            ident = json.loads(ident)
        except ValueError:
            return ident
    if isinstance(ident, dict):
        return json.dumps(ident, sort_keys=True, separators=(",", ":"))
    return str(ident)


def _obj_name(key, typ, names):
    """Short name for the cleanup summary: alias or TYPE#n, or TYPE plus the key fields."""
    if key.startswith("{"):
        try:
            fields = json.loads(key)
        except ValueError:
            return f"{typ} {key}"
        body = " ".join(f"{k}={names.value(v)}" for k, v in fields.items() if k != "switch_id")
        return f"{typ} {body}"
    name = names.name(key)
    return f"{typ} {name}" if name.startswith("oid:") else name


def _verdict(info, checks, stats):
    result = info["result"]
    n_assert = sum(c["n"] for c in checks if c["kind"] == "assert" and c["ok"])
    n_pkt = sum(1 for c in checks if c["kind"] == "pkt" and c["ok"])
    caught = sum(1 for c in checks if c["kind"] == "pkt" and not c["ok"])
    calls = f"{stats['sai']} SAI calls" + (f", {stats['bad']} not SUCCESS" if stats["bad"] else "")
    if stats.get("reset_calls"):
        calls += f" (plus {stats['reset_calls']} during a switch reset)"
    if result == "Pass":
        extra = f"; {caught} packet check(s) did not match and the test handled it" if caught else ""
        return f"Pass. {n_assert} assert(s) and {n_pkt} packet check(s) passed{extra}; {calls}."
    if result == "Interrupted":
        return f"Interrupted: {info['detail']}; {calls} recorded before the stop."
    if result == "Skipped":
        rep = info["reports"].get("call") or info["reports"].get("setup") or {}
        return f"Skipped: {_skip_reason(rep)}."
    if result == "Expected gap":
        return f"Expected gap: the test failed as the target profile expects; {calls}."
    if result == "Gap now passes":
        return "Gap now passes: the test passed although the profile lists it as an expected gap (strict xfail)."
    if result == "Setup error":
        return "Setup error: the test body did not run."
    if result == "Cleanup error":
        return f"Cleanup error: the test body passed, its teardown failed; {calls}."
    return f"Fail. {calls}."


def _steps(info, names):
    """Readable step lines (grouped by phase), the checks list and per-test counters."""
    t0 = _ts(info["test"]) if info["test"] else (_ts(info["own"][0]) if info["own"] else 0.0)
    checks = []
    stats = {"sai": 0, "bad": 0, "created": set(), "removed": set(), "surface": {}, "pcaps": [],
             "last_bad_sai": None, "reset_calls": 0, "types": {}, "names": {}}
    blocks = []                 # [header, [lines]] per phase; empty phases are dropped
    events = info["own"]

    # fold size probes: a BUFFER_OVERFLOW get followed by the same get
    views = {e.get("seq"): sai_view(e) for e in events if e.get("k") == "sai"}
    skip = _probes([(e.get("seq"), views[e.get("seq")]) for e in events if e.get("k") == "sai"])

    failing = _failing_report(info)
    fail_msg = _first_line(((failing or {}).get("exc") or {}).get("msg")) if failing else ""
    state = {"phase": None, "fixture": None, "reset": False}
    group = []                  # consecutive SAI calls with the same signature
    reinit = []                 # calls after a switch reset (the switch's own re-init)
    nothing = []                # consecutive "nothing on port" polls

    def out(line):
        blocks[-1][1].append(line)

    def flush_group():
        if reinit:
            for line in _collapse(reinit, names):
                out(line)
            reinit.clear()
        if len(group) <= 3:
            for off, text, _ in group:
                out(f"  {off} {text}")
        else:
            out(f"  {group[0][0]} {group[0][1]}")
            out(f"  {'':9} … {len(group) - 2} more calls like this …")
            out(f"  {group[-1][0]} {group[-1][1]}")
        group.clear()

    def flush_nothing():
        if nothing:
            ports = [str(p["port"]) for p in nothing if p["port"] != "any"]
            where = []
            if ports:
                where.append(f"port(s) {','.join(ports)}")
            if len(ports) < len(nothing):
                where.append("any other port")
            tmo = nothing[0].get("timeout")
            out(f"  {nothing[0]['off']} nothing on {' and '.join(where)}"
                + (f" ({tmo} s each)" if tmo is not None else "") + ", as expected")
            nothing.clear()

    def flush_all():
        flush_group()
        flush_nothing()

    for e in events:
        k = e.get("k")
        if k in ("test", "report", "test_end", "fixture_done", "collected", "client"):
            continue
        if k == "aliases":
            names.set_base(e.get("map"))
            continue
        phase = e.get("phase")
        if phase != state["phase"]:
            if blocks:
                flush_all()
            state.update(phase=phase, fixture=None, reset=False)
            blocks.append([{"setup": "setup", "call": "test body", "teardown": "cleanup"}.get(phase, phase or "?"),
                           []])
        off = f"+{_ts(e) - t0:7.3f}s"
        fx = e.get("fx")
        if phase == "setup" and fx != state["fixture"]:
            flush_all()
            state["fixture"], state["reset"] = fx, False
            if fx:
                out(f"  [fixture {fx}]")
        if k != "sai":
            flush_group()
        if k != "poll":
            flush_nothing()

        if k == "sai":
            if e.get("seq") in skip:
                continue
            v = views.get(e.get("seq")) or sai_view(e)
            if v["op"] == "cleanup":
                state["reset"] = True
            text = sai_line(v, names, baseline=state["reset"])
            if v.get("after_probe"):
                text += "  (after a size probe)"
            if state["reset"]:
                stats["reset_calls"] += 1     # switch re-init after a reset: baseline, not the test's calls
            elif v["op"] != "cleanup":
                stats["sai"] += 1
            if v["status"] not in (OK, None) and v["op"] != "cleanup":
                stats["bad"] += 1
                stats["last_bad_sai"] = text
            if v["exc"]:
                stats["last_bad_sai"] = text
            typ = _view_type(v, names)
            if v["op"] == "create" and v["status"] == OK and not state["reset"]:
                oid = _okey(v["created"] or v["ident"])
                stats["created"].add(oid)
                stats["types"][oid] = typ
                stats["names"][oid] = _obj_name(oid, typ, names)
            if v["op"] == "remove" and v["status"] == OK and v["ident"]:
                oid = _okey(v["ident"])
                stats["removed"].add(oid)
                stats["types"].setdefault(oid, typ)
                # name now: a later alias update can point the same name at a replacement object
                stats["names"].setdefault(oid, _obj_name(oid, typ, names))
            if v["op"] != "cleanup" and not state["reset"]:
                s = stats["surface"].setdefault(typ, {"ops": {}, "attrs": set()})
                s["ops"][v["op"]] = s["ops"].get(v["op"], 0) + 1
                if v["op"] in ("create", "set", "get"):
                    s["attrs"].update(k2 for k2, _ in v["attrs"])
            if state["reset"] and v["op"] != "cleanup":
                reinit.append((off, v, text))
                continue
            sig = _sig(v)
            if group and group[-1][2] != sig:
                flush_group()
            group.append((off, "  " * int(e.get("depth") or 0) + text, sig))
            if v["op"] == "cleanup":
                flush_group()
        elif k == "wire":
            out(f"  {off} wire {e.get('fn')} {_short(e.get('op') or e.get('operation') or '')}"
                f" -> {e.get('status') or (e.get('exc') or {}).get('type')}")
        elif k == "assert":
            file = Path(str(e.get("file") or "?")).name
            where = f"`{file}:{e.get('line')}`"
            check = f"`{_md_cell(_short(e.get('orig'), 100))}`"
            if checks and checks[-1]["kind"] == "assert" and checks[-1]["where"] == where \
                    and checks[-1]["check"] == check and checks[-1]["ok"]:
                checks[-1]["n"] += 1
                checks[-1]["result"] = f"PASS ×{checks[-1]['n']}"
                continue
            checks.append({"kind": "assert", "ok": True, "result": "PASS", "n": 1, "where": where,
                           "check": check, "values": f"`{_md_cell(_first_line(e.get('expl'), 160))}`"})
            out(f"  {off} CHECK PASS {file}:{e.get('line')}  {_short(e.get('orig'), 100)}")
        elif k == "pktcheck":
            if e.get("ok"):
                res = "PASS"
            elif info["result"] == "Pass" or not (fail_msg and fail_msg in str(e.get("msg") or "")):
                res = "not as expected (caught by the test)"
            else:
                res = "FAIL"
            func = e.get("func") or "?"
            if func.startswith("verify_no"):
                desc = f"{func}: nothing on {_ports(e.get('ports'))}"
            else:
                desc = f"{func}: expect on {_ports(e.get('ports'))}"
                if func == "verify_packets":
                    desc += ", nothing elsewhere"
            if e.get("count") is not None:
                desc += f" (matched {e['count']})"
            exp = e.get("exp")
            checks.append({"kind": "pkt", "ok": bool(e.get("ok")), "result": res, "where": "ptf", "func": func,
                           "check": _md_cell(desc), "values": _md_cell(_pkt(exp) if exp else "")})
            if res == "FAIL":
                stats["pkt_miss"] = stats.get("last_miss")
            out(f"  {off} CHECK {res} {desc}" + (f"  [{_pkt(exp)}]" if exp else ""))
            if not e.get("ok") and e.get("msg"):
                for ln in str(e["msg"]).splitlines()[:40]:
                    out("      " + ln)
        elif k == "send":
            out(f"  {off} SEND port {e.get('port')} {_pkt(e)}")
        elif k == "poll":
            negative = str(e.get("within") or "").startswith("verify_no")
            if e.get("ok"):
                flush_nothing()
                got = e.get("got") or {}
                note = " (UNEXPECTED: a negative check)" if negative else \
                    (" (matches expected)" if e.get("exp") else "")
                out(f"  {off} RECV port {got.get('port')} {_pkt(got)}{note}")
            elif negative or (e.get("exp") is None and e.get("port") is not None):
                nothing.append({"port": e.get("port") if e.get("port") is not None else "any",
                                "timeout": e.get("timeout"), "off": off})
            else:
                flush_nothing()
                where = f"port {e.get('port')}" if e.get("port") is not None else "any port"
                out(f"  {off} nothing{' matching' if e.get('exp') else ''} on {where}"
                    f" within {e.get('timeout')} s ({e.get('count', 0)} other frame(s) seen)")
                stats["last_miss"] = {"where": where, "timeout": e.get("timeout"), "recent": e.get("recent") or []}
                for p in e.get("recent") or []:
                    out(f"      seen: {_pkt(p)}")
                    if e.get("exp"):
                        for ln in _hexdump(p.get("hex")):
                            out(ln)
        elif k == "pcap":
            stats["pcaps"].append(e)
            out(f"  {off} pcap written: {_rel(e.get('path'))} ({e.get('frames')} frames)")
        elif k == "note":
            out(f"  {off} note: {e.get('text')}")
    if blocks:
        flush_all()

    # the failing check (pytest reports it; the pass hook only sees passing asserts)
    rep = failing
    if rep is not None and info["result"] in ("Fail", "Expected gap", "Setup error", "Cleanup error"):
        exc = rep.get("exc") or {}
        where = rep.get("where") or {}
        if where:
            src = _short(where.get("src") or "", 100)
            file = Path(str(where.get("file") or "?")).name
            etype = exc.get("type")
            label = "FAIL" if etype in (None, "AssertionError") else f"ERROR ({etype})"
            if info["result"] == "Expected gap":
                label += " (expected gap)"
            pkt = next((c for c in reversed(checks) if c["kind"] == "pkt" and c["result"] == "FAIL"), None)
            if pkt is not None and pkt.get("func") and pkt["func"] in src:
                # the failing packet check is the test line's failure: one row, with the test line
                pkt["where"], pkt["result"] = f"`{file}:{where.get('line')}`", label
                return _steps_lines(blocks), checks, stats
            checks.append({"kind": "assert" if src.lstrip().startswith("assert") else "sai", "ok": False,
                           "result": label, "where": f"`{file}:{where.get('line')}`",
                           "check": f"`{_md_cell(src)}`" if src else "(SAI helper)",
                           "values": f"`{_md_cell(_first_line(exc.get('msg'), 160))}`"})
            line = f"  +{_ts(rep) - t0:7.3f}s CHECK {label} {file}:{where.get('line')}  {src}"
            when = {"setup": "setup", "call": "test body", "teardown": "cleanup"}.get(rep.get("when"))
            target = next((b for b in blocks if b[0] == when), None)
            if target is None:
                blocks.append([when or "?", []])
                target = blocks[-1]
            target[1].append(line)

    return _steps_lines(blocks), checks, stats


def _steps_lines(blocks):
    lines = []
    for header, body in blocks:
        if body:
            lines.append(header)
            lines.extend(body)
    return lines


def _alias_type(names, ident):
    name = names.base.get(ident) if isinstance(ident, str) else None
    if not name:
        return None
    for prefix, typ in (("port[", "PORT"), ("bp[", "BRIDGE_PORT"), ("switch", "SWITCH"),
                        ("default_vlan", "VLAN"), ("default_vrf", "VIRTUAL_ROUTER"), ("1q_bridge", "BRIDGE")):
        if name.startswith(prefix):
            return typ
    return None


def _md_cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


# ---------------------------------------------------------------- whole run

def _group_session(events, aliases):
    """Session-level SAI calls (switch init, final teardown): size probes folded, runs of
    successful gets grouped. Returns (lines, number of size probes)."""
    names = Names(aliases)
    calls = [e for e in events if e.get("k") == "sai"]
    views = {e.get("seq"): sai_view(e) for e in calls}
    skip = _probes([(e.get("seq"), views[e.get("seq")]) for e in calls])
    t0 = _ts(events[0]) if events else 0.0
    items = []
    for e in events:
        if e.get("k") == "aliases":
            names.set_base(e.get("map"))
            continue
        if e.get("k") != "sai" or e.get("seq") in skip:
            continue
        v = views[e.get("seq")]
        items.append((f"+{_ts(e) - t0:7.3f}s", v, sai_line(v, names)))
    return _collapse(items, names), len(skip)


def render_markdown(events):
    session = next((e for e in events if e.get("k") == "session"), {}) or {}
    clients = [e for e in events if e.get("k") == "client"]
    collected = next((e.get("n") for e in events if e.get("k") == "collected"), None)
    run_end = next((e for e in events if e.get("k") == "run_end"), None)

    # bucket events per test; aliases are global state in seq order
    tests, order = {}, []
    aliases = {}
    alias_at_start = {}
    first_aliases = None
    session_events = []
    for e in events:
        if e.get("k") == "aliases":
            aliases = merge_aliases(aliases, e.get("map"))
        elif first_aliases is None and aliases and e.get("phase") == "call":
            first_aliases = dict(aliases)   # the baseline once switch init is done
        nodeid = e.get("test")
        if nodeid:
            if nodeid not in tests:
                tests[nodeid] = []
                order.append(nodeid)
                alias_at_start[nodeid] = dict(aliases)
            tests[nodeid].append(e)
        elif e.get("k") in ("sai", "aliases", "note", "wire"):
            session_events.append(e)

    infos = []
    for nodeid in order:
        info = analyse_test(tests[nodeid], alias_at_start[nodeid])
        session_events.extend(info["session"])
        infos.append((nodeid, info))
    session_events.sort(key=lambda e: e.get("seq", 0))

    out = []
    run = session.get("run") or "?"
    out.append(f"# Test log: {run}")
    out.append("")
    out.append("_Draft for internal review. Generated automatically from the run's events "
               f"(`{run}.events.jsonl`); every line below was captured during the run._")
    out.append("")
    hdr = [
        ("Target profile", session.get("profile")),
        ("Testbed", session.get("testbed")),
        ("Traffic", "on" if session.get("traffic") else "off"),
        ("MLD filter", "on" if session.get("mld") else "off"),
        ("Expected gaps in profile", session.get("gaps")),
        ("SAI client", ", ".join(sorted({str(c.get('cls')) for c in clients})) or "-"),
        ("Git commit (tests/poc)", session.get("commit") or "unknown"),
        ("Started", session.get("start")),
        ("Python / pytest / pytest-html", (f"{session.get('python')} / {session.get('pytest')} / "
                                           f"{session.get('pytest_html')}")),
        ("ptf / scapy", f"{session.get('ptf')} / {session.get('scapy')}"),
        ("saichallenger package", session.get("saichallenger")),
        ("Tests collected", collected),
    ]
    out.append("| | |")
    out.append("|---|---|")
    out.extend(f"| {k} | {_md_cell(v)} |" for k, v in hdr if v not in (None, ""))
    out.append("")

    counts = {}
    for _, info in infos:
        counts[info["result"]] = counts.get(info["result"], 0) + 1
    out.append("**Results:** " + ", ".join(f"{r} {counts[r]}" for r in RESULT_ORDER if r in counts)
               + (f" (pytest exit status {run_end.get('exitstatus')})" if run_end else " (run did not finish)"))
    out.append("")
    out.append("| # | Test | Result | Why | SAI calls | Checks | Time |")
    out.append("|---|---|---|---|---|---|---|")
    sections = []
    for i, (nodeid, info) in enumerate(infos, 1):
        text, _ = _render_info(info, number=i, anchor=f"t{i}")
        sections.append(text)
        why = _why(info)
        n_sai, n_chk = info.get("n_sai", 0), info.get("n_checks", 0)
        out.append(f"| {i} | [{info['test'].get('name') or nodeid}](#t{i}) | {info['result']} | {_md_cell(why)} | "
                   f"{n_sai} | {n_chk} | {info['duration']:.2f} s |")
    out.append("")

    out.append("## Session start and end")
    out.append("")
    base = first_aliases or aliases or {}
    if base:
        ports = sorted((v, k) for k, v in base.items() if v.startswith("port["))
        bps = sorted((v, k) for k, v in base.items() if v.startswith("bp["))
        others = sorted((v, k) for k, v in base.items() if not v.startswith(("port[", "bp[")))
        out.append(f"Baseline after switch init: {len(ports)} ports, {len(bps)} bridge ports; "
                   + ", ".join(f"{v} = `{k}`" for v, k in others) + ".")
        out.append("")
        out.append("<details><summary>Port and bridge-port names used below</summary>\n\n```text")
        for (pv, pk), (bv, bk) in zip(sorted(ports, key=lambda x: _idx(x[0])), sorted(bps, key=lambda x: _idx(x[0]))):
            out.append(f"{pv:9} {pk:24} {bv:7} {bk}")
        out.append("```\n</details>")
        out.append("")
    sess_lines, n_probe = _group_session(session_events, base)
    n_sess = sum(1 for e in session_events if e.get("k") == "sai")
    out.append(f"{n_sess} SAI calls outside the tests (switch init, resets by session fixtures, final teardown)"
               + (f", including {n_probe} size probes (a get answered {PROBE} with the list length, "
                  "then repeated with room for the list; shown once)" if n_probe else "")
               + ". Runs of successful gets are grouped with a count per attribute; creates, sets, removes "
               "and every call that did not succeed are listed one by one.")
    out.append("")
    out.append("<details><summary>Session SAI calls</summary>\n\n```text")
    out.extend(sess_lines or ["(none recorded)"])
    out.append("```\n</details>")
    out.append("")
    out.append("## Tests")
    out.append("")
    out.extend(sections)
    return "\n".join(out) + "\n"


def _idx(name):
    m = re.search(r"\[(\d+)\]", name)
    return int(m.group(1)) if m else 0


def _why(info):
    res = info["result"]
    if res == "Pass":
        return ""
    if res == "Interrupted":
        return info["detail"]
    if res == "Skipped":
        rep = info["reports"].get("call") or info["reports"].get("setup") or {}
        return _skip_reason(rep)
    rep = _failing_report(info) or {}
    msg = _first_line((rep.get("exc") or {}).get("msg") or rep.get("longrepr"), 120)
    if res == "Expected gap":
        reason = (info["test"].get("xfail") or {}).get("reason")
        return reason or msg
    return msg


# ---------------------------------------------------------------- sairedis.rec

_REDIS_OPS = {"Screate": "c", "Dremove": "r", "Sset": "s", "Sget": "g"}


def _attr_text(pairs):
    return "|".join(f"{k}={'' if v is None else v}" for k, v in pairs)


def rec_lines(events):
    """sairedis.rec-format lines (Recorder.cpp grammar); test markers and checks as '#|' comments."""
    lines = []
    oid_type = {}
    names = Names({})
    tests_seen = set()
    last_fx = [None]

    def add(ts, body):
        lines.append(f"{_rec_time(ts)}|{body}")

    for e in events:
        k, ts = e.get("k"), _ts(e)
        if k == "session":
            add(ts, f"#|recording on: {e.get('run')}.rec (SAI-Challenger PoC test log, "
                    f"profile {e.get('profile')}, testbed {e.get('testbed')})")
        elif k == "test":
            tests_seen.add(e.get("nodeid"))
            add(ts, f"#|START {e.get('nodeid')}")
        elif k == "test_end":
            add(ts, f"#|END {e.get('nodeid')} {e.get('result') or ''}".rstrip())
        elif k == "assert":
            add(ts, f"#|CHECK PASS {Path(str(e.get('file'))).name}:{e.get('line')} {e.get('orig')}")
        elif k == "pktcheck":
            add(ts, f"#|PKTCHECK {'PASS' if e.get('ok') else 'NOT-AS-EXPECTED'} {e.get('func')} "
                    f"ports={_ports(e.get('ports'))}")
        elif k == "send":
            add(ts, f"#|PKT SEND port {e.get('port')} {e.get('len')}B {e.get('sum')}")
        elif k == "poll" and e.get("ok"):
            got = e.get("got") or {}
            add(ts, f"#|PKT RECV port {got.get('port')} {got.get('len')}B {got.get('sum')}")
        elif k == "sai":
            if e.get("fx") != last_fx[0]:
                last_fx[0] = e.get("fx")
                if last_fx[0]:
                    add(ts, f"#|fixture {last_fx[0]} ({e.get('fxs')} scope, {e.get('phase')})")
            for body in _sai_rec(e, oid_type, names):
                add(ts, body)
        elif k == "report" and e.get("outcome") == "failed":
            msg = (e.get("exc") or {}).get("msg") or e.get("longrepr")
            add(ts, f"#|FAILED {e.get('when')} {_first_line(msg, 200)}")
    return lines


def _sai_rec(e, oid_type, names):
    op = e.get("op")
    wire = [w for w in (e.get("wire") or []) if isinstance(w, dict)]
    out = []
    if op == "cleanup":
        return ["#|switch reset (client cleanup)"]
    redis = [w for w in wire if w.get("fn") == "operate"]
    if redis:
        for w in redis:
            code = _REDIS_OPS.get(w.get("op"))
            key = w.get("key") or ""
            pairs = _pairs(w.get("attrs"))
            status = w.get("status")
            if code == "c":
                out.append(f"c|{key}" + (f"|{_attr_text(pairs)}" if pairs else ""))
            elif code == "r":
                out.append(f"r|{key}")
            elif code == "s":
                out.append(f"s|{key}|{_attr_text(pairs)}")
            elif code == "g":
                out.append(f"g|{key}|{_attr_text(pairs)}")
                out.append(f"G|{status}" + (f"|{_attr_text(_pairs(w.get('data')))}" if status == OK else ""))
                continue
            elif w.get("op") in ("Sget_stats", "Sclear_stats"):
                name = "get_stats" if w.get("op") == "Sget_stats" else "clear_stats"
                out.append(f"q|{name}|{key}|{_attr_text(pairs)}")
                out.append(f"Q|{name}|{status}" + (f"|{_attr_text(_pairs(w.get('data')))}"
                                                   if name == "get_stats" and status == OK else ""))
                continue
            elif w.get("op") == "Sflush":
                out.append(f"f|{key}|{_attr_text(pairs)}")
                out.append(f"F|{status}")
                continue
            else:
                out.append(f"#|{w.get('op')} {key} (not written in sairedis grammar)")
                continue
            if status and status != OK:
                out.append(f"E|{status}")
        return out
    # no `operate` wire event (other client shape): rebuild from the API call
    v = sai_view(e)
    typ = v["typ"]
    ident = v["created"] or v["ident"]
    if isinstance(ident, str) and ident.startswith("oid:"):
        typ = typ or oid_type.get(ident)
        if typ:     # learned from a create, or from an attribute name (SAI_<TYPE>_ATTR_...)
            oid_type.setdefault(ident, typ)
    key = f"SAI_OBJECT_TYPE_{typ or 'UNKNOWN'}:{ident if ident is not None else ''}"
    attrs = _attr_text(v["attrs"])
    status = v["status"]
    if op == "create":
        out.append(f"c|{key}" + (f"|{attrs}" if attrs else ""))
    elif op == "remove":
        out.append(f"r|{key}")
    elif op == "set":
        out.append(f"s|{key}|{attrs}")
    elif op == "get":
        out.append(f"g|{key}|{attrs}")
        out.append(f"G|{status}" + (f"|{_attr_text(v['read'])}" if status == OK else ""))
        return out
    elif op in ("get_stats", "clear_stats"):
        out.append(f"q|{op}|{key}|{attrs}")
        out.append(f"Q|{op}|{status}" + (f"|{_attr_text(v['read'])}" if op == "get_stats" and status == OK else ""))
        return out
    elif op == "flush_fdb_entries":
        out.append(f"f|{key}|{attrs}")
        out.append(f"F|{status}" if status else "#|F no status returned by the client")
        return out
    else:
        out.append(f"#|{op} {key} (not written in sairedis grammar)")
        return out
    if status and status != OK:
        out.append(f"E|{status}")
    return out


# ---------------------------------------------------------------- files

def outputs_for(jsonl_path):
    p = Path(jsonl_path)
    base = p.name[: -len(".events.jsonl")] if p.name.endswith(".events.jsonl") else p.stem
    return p.with_name(base + ".testlog.md"), p.with_name(base + ".rec")


def write_outputs(jsonl_path):
    """Render <base>.testlog.md and <base>.rec next to <base>.events.jsonl; returns both paths."""
    events = load(jsonl_path)
    md_path, rec_path = outputs_for(jsonl_path)
    md_path.write_text(render_markdown(events), encoding="utf-8")
    rec_path.write_text("\n".join(rec_lines(events)) + "\n", encoding="utf-8")
    return md_path, rec_path


def main(argv):
    if len(argv) != 2:
        print("usage: python3 -I testlog_render.py <run>.events.jsonl", file=sys.stderr)
        return 2
    md_path, rec_path = write_outputs(argv[1])
    print(f"test log: {md_path}\nrec:      {rec_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
