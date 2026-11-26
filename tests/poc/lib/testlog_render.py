"""Render a PoC run's events (<run>.events.jsonl) as one plain-text log per test, an index, and a
sairedis.rec-style file.

PYTEST_DONT_REWRITE: pytest must not rewrite this module (it contains no assert statements).

Standard library only and no saichallenger import, so it also runs on the host, for example
after an interrupted run:

    python3 -I tests/poc/lib/testlog_render.py tests/poc/reports/<run>.events.jsonl

writes, next to the .jsonl:
    <run>/<profile>-<mmdd-hhmm>_<NN>_<test>.log   one per test, in run order
    <run>/<profile>-<mmdd-hhmm>_index.log         the run header and one line per test
    <run>.rec                                     every SAI call in sairedis.rec grammar
tests/poc/lib/testlog.py writes the events.
"""
import collections
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
ATTR_RE = re.compile(r"^SAI_([A-Z0-9_]+?)_ATTR_([A-Z0-9_]+)$")
LIST_RE = re.compile(r"^(\d+):(oid:|SAI_|null$|$)")
STAMP_RE = re.compile(r"^(.*-\d{4}-\d{4})\d{2}$")
MAX_VALUE = 160
VALUE_W = 48            # longest value shown in a step line
RESULT_COL_MAX = 92     # results are aligned to a column, at most this far right
LABEL_W = 17

RESULT_ORDER = ("Interrupted", "Fail", "Setup error", "Cleanup error", "Gap now passes", "Expected gap", "Skipped", "Pass")
CLIENT_PATH = {"SaiRedisClient": "Redis ASIC_STATE queue -> syncd -> libsai",
               "SaiThriftClient": "Thrift RPC -> saiserver -> libsai"}
WHY_TITLE = {"Fail": "FAILED", "Expected gap": "EXPECTED GAP", "Gap now passes": "GAP NOW PASSES",
             "Skipped": "SKIPPED", "Setup error": "SETUP ERROR", "Cleanup error": "CLEANUP ERROR",
             "Interrupted": "INTERRUPTED"}


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


def _clock(ts):
    """Wall-clock time of an event, HH:MM:SS.mmm (local time, as in the .rec)."""
    return time.strftime("%H:%M:%S", time.localtime(ts)) + f".{round((ts % 1) * 1000) % 1000:03d}"


def _rec_time(ts):
    lt = time.localtime(ts)
    return time.strftime("%Y-%m-%d.%H:%M:%S.", lt) + f"{round((ts % 1) * 1e6) % 1000000:06d}"


def _rel(path):
    """Show a path from tests/poc_<user>/ onwards (container) or tests/poc/ (host)."""
    path = str(path or "")
    m = re.search(r"(tests/poc[^/]*/.*)$", path)
    return m.group(1) if m else path


def _base(path):
    return str(path or "?").replace("\\", "/").rsplit("/", 1)[-1]


def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def _align(rows, cap=RESULT_COL_MAX):
    """rows: (left, right or None). The rights are lined up in one column."""
    widths = [len(left) for left, right in rows if right is not None and len(left) + 3 <= cap]
    col = max(widths) + 3 if widths else 0
    out = []
    for left, right in rows:
        if right is None:
            out.append(left.rstrip())
        elif len(left) + 3 > col:
            out.append(f"{left}   {right}")
        else:
            out.append(left.ljust(col) + right)
    return out


def _wrap(words, width, first, rest):
    """Join words into lines of at most width characters; first/rest are the line prefixes."""
    lines, cur, empty = [], first, True
    for w in words:
        if not empty and len(cur) + 1 + len(w) > width:
            lines.append(cur.rstrip())
            cur, empty = rest, True
        cur = cur + w if empty else cur + " " + w
        empty = False
    if not empty:
        lines.append(cur.rstrip())
    return lines


def file_stem(run, index, width, name):
    """Base name of a test's files: <profile>-<mmdd-hhmm>_<NN>_<test>. The run name carries seconds
    (<profile>-<mmdd-hhmmss>); the files leave them out."""
    run = str(run or "run")
    m = STAMP_RE.match(run)
    prefix = m.group(1) if m else run
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name))
    return f"{prefix}_{int(index):0{int(width)}d}_{safe}"


def index_name(run):
    run = str(run or "run")
    m = STAMP_RE.match(run)
    return f"{m.group(1) if m else run}_index.log"


# ---------------------------------------------------------------- short SAI names

def split_attr(name):
    """'SAI_VLAN_MEMBER_ATTR_VLAN_ID' -> ('VLAN_MEMBER', 'VLAN_ID'); anything else -> (None, name)."""
    m = ATTR_RE.match(str(name))
    return (m.group(1), m.group(2)) if m else (None, str(name))


def short_attr(name):
    return split_attr(name)[1]


def _enum_short(value, attr):
    """Drop the enum type from a value, using its attribute's name: SAI_VLAN_TAGGING_MODE_UNTAGGED for
    VLAN_MEMBER.VLAN_TAGGING_MODE -> UNTAGGED, SAI_FDB_ENTRY_TYPE_STATIC for FDB_ENTRY.TYPE -> STATIC."""
    typ, short = split_attr(attr)
    words = short.split("_")
    if len(words) > 1 and words[-1] == "LIST":
        words = words[:-1]
    prefixes = []
    for i in range(len(words)):
        tail = "_".join(words[i:])
        prefixes.append(f"SAI_{tail}_")
        if typ:
            prefixes.append(f"SAI_{typ}_{tail}_")
    for p in sorted(prefixes, key=len, reverse=True):
        if value.startswith(p) and len(value) > len(p):
            return value[len(p):]
    return None


# Per-run caches for the pytest-html block, which renders one test at a time from the events so far:
# events never change once written, so what was learned from earlier events is kept.
_CACHE = {}


def _run_cache(run):
    if len(_CACHE) > 4 and run not in _CACHE:
        _CACHE.clear()
    return _CACHE.setdefault(run, {"enums_upto": 0, "enums": {}, "init": {}, "types_upto": 0, "types": {}, "seen": {}})


def learn_enums(events, cache=None):
    """Short names for every enum value sent with an attribute in this run (used where a value
    appears without its attribute, e.g. npu.create_vlan_member(..., SAI_VLAN_TAGGING_MODE_TAGGED))."""
    enums = cache["enums"] if cache is not None else {}
    upto = cache["enums_upto"] if cache is not None else 0
    for e in events:
        if e.get("k") != "sai" or e.get("seq", 0) <= upto:
            continue
        args = e.get("args") or {}
        for k, v in _pairs(args.get("attrs", args.get("attr"))):
            for item in _list_items(v):
                if isinstance(item, str) and item.startswith("SAI_") and item not in enums:
                    s = _enum_short(item, k)
                    if s:
                        enums[item] = s
    if cache is not None and events:
        cache["enums_upto"] = max(upto, events[-1].get("seq", 0))
    return enums


def learn_types(events, cache=None):
    """Object type of each OID, from what the run shows about it: the key of the create that made it, or
    the SAI_<TYPE>_ATTR_ names of a get or set on it. Some clients (thrift) send a remove with the OID
    only; this names what they removed. OIDs seen only in removes stay without a type."""
    types = cache["types"] if cache is not None else {}
    seen = cache["seen"] if cache is not None else {}       # oid -> "TYPE.ATTR" it was first read from
    upto = cache["types_upto"] if cache is not None else 0
    for e in events:
        if e.get("k") != "sai" or e.get("seq", 0) <= upto or e.get("op") in ("flush_fdb_entries", "cleanup"):
            continue
        v = sai_view(e)
        if not v["typ"]:
            continue
        for k, val in v["read"]:
            for item in _list_items(val) if isinstance(val, str) else []:
                if isinstance(item, str) and item.startswith("oid:") and item != "oid:0x0":
                    seen.setdefault(item, f"{v['typ']}.{short_attr(k)}")
        oid = v["created"] or v["ident"]
        if isinstance(oid, str) and oid.startswith("oid:"):
            types.setdefault(oid, v["typ"])
    if cache is not None and events:
        cache["types_upto"] = max(upto, events[-1].get("seq", 0))
    return types


def _list_items(v):
    if not isinstance(v, str):
        return [v]
    m = LIST_RE.match(v)
    if m:
        body = v[len(m.group(1)) + 1:]
        return [x for x in body.split(",") if x and x != "null"]
    return [v]


class Names:
    """Readable names: baseline aliases (switch, port[i], bp[i], default_vlan, ...) from the recorder,
    TYPE#n for objects a test creates, and short attribute and enum names."""

    def __init__(self, aliases=None, enums=None):
        self.base = dict(aliases or {})
        self.local = {}
        self.types = {}       # oid -> object type (from creates)
        self.counts = {}
        self.shown = set()
        self.enums = enums if enums is not None else {}
        self.extra = {}       # oid -> name of the switch attribute it was read from (e.g. cpu_port)

    def learn_switch_values(self, seen):
        """OIDs the run read from a single-value switch attribute get that attribute's name."""
        for oid, src in seen.items():
            if src.startswith("SWITCH.") and not src.endswith("_LIST"):
                self.extra.setdefault(oid, src.split(".", 1)[1].lower())

    def set_base(self, aliases):
        self.base = merge_aliases(self.base, aliases)

    def created(self, typ, oid):
        if not oid or not isinstance(oid, str) or not oid.startswith("oid:"):
            return None
        if oid in self.base:
            self.types[oid] = typ
            return self.base[oid]
        if oid in self.local:
            return self.local[oid]
        self.counts[typ] = self.counts.get(typ, 0) + 1
        name = f"{typ or 'OBJ'}#{self.counts[typ]}"
        self.local[oid] = name
        self.types[oid] = typ
        return name

    def name(self, oid, first_use=False):
        name = self.local.get(oid) or self.base.get(oid) or self.extra.get(oid)
        if not name:
            return oid
        if oid in self.extra and oid not in self.local and oid not in self.base and oid not in self.shown:
            self.shown.add(oid)
            return f"{name} {oid}"
        if first_use and oid in self.local and oid not in self.shown:
            self.shown.add(oid)
            return f"{name} {oid}"
        return name

    def enum(self, s, attr=None):
        if s.startswith("SAI_STATUS_"):
            return s[len("SAI_STATUS_"):]
        if s.startswith("SAI_OBJECT_TYPE_"):
            return s[len("SAI_OBJECT_TYPE_"):]
        if s in self.enums:
            return self.enums[s]
        if attr:
            short = _enum_short(s, attr)
            if short:
                self.enums[s] = short
                return short
        return s

    def obj(self, v):
        """An object argument: an OID, 'SAI_OBJECT_TYPE_X:<oid or entry key>', or an entry key."""
        if isinstance(v, dict):
            return self.entry(v)
        if isinstance(v, str):
            if v.startswith("SAI_OBJECT_TYPE_"):
                typ, rest = _type_of_key(v)
                if rest and rest.startswith("{"):
                    return self.entry(rest, typ)
                if rest and rest.startswith("oid:"):
                    return self.name(rest)
                return typ
            if v.startswith("{"):
                return self.entry(v)
        return self.value(v)

    def entry(self, key, typ=None):
        """An entry key ({"bvid": .., "mac": .., "switch_id": ..}) as TYPE(value, value)."""
        if isinstance(key, str):
            try:
                key = json.loads(key)
            except ValueError:
                return f"{typ or 'ENTRY'}({_short(key, VALUE_W)})"
        if not isinstance(key, dict):
            return str(key)
        vals = [self.value(v) for k, v in key.items() if k != "switch_id"]
        return f"{typ}(" + ", ".join(vals) + ")" if typ else "(" + ", ".join(vals) + ")"

    def value(self, v, attr=None, limit=VALUE_W):
        if v is None:
            return "None"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, dict):
            if "_saidata" in v:
                return ", ".join(f"{short_attr(k)}={self.value(x, k)}" for k, x in _pairs(v["_saidata"]))
            return self.entry(v)
        if isinstance(v, list):
            shown = [self.value(x, attr) for x in v[:6]]
            return "[" + ", ".join(shown) + (", …" if len(v) > 6 else "") + "]"
        s = str(v)
        m = LIST_RE.match(s)
        if m:
            n = int(m.group(1))
            items = _list_items(s)
            if n == 0:
                return "[]"
            if n <= 3 and len(items) == n:
                return "[" + ", ".join(self.value(x, attr) for x in items) + "]"
            return f"list of {n}"
        if s.startswith("oid:"):
            return self.name(s)
        if s.startswith("SAI_OBJECT_TYPE_") and ":" in s:
            return self.obj(s)
        if s.startswith("SAI_"):
            return self.enum(s, attr)
        if s.startswith("{"):
            return self.entry(s)
        text = OID_RE.sub(lambda mm: self.local.get(mm.group(0)) or self.base.get(mm.group(0)) or mm.group(0), s)
        return _short(text, limit)

    def attrs(self, pairs, values=True):
        if values:
            return " ".join(f"{short_attr(k)}={self.value(v, k)}" for k, v in pairs)
        return ", ".join(short_attr(k) for k, _ in pairs)


def merge_aliases(old, new):
    """Alias map after an 'aliases' event. A switch re-init reports partial maps while the lists
    fill in, so known names are kept; a name that now points to another OID drops the old one."""
    new = dict(new or {})
    moved = set(new.values())
    merged = {oid: name for oid, name in (old or {}).items() if name not in moved}
    merged.update(new)
    return merged


# ---------------------------------------------------------------- SAI client calls

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


def _sai_text(v, names):
    """A SAI client call made outside any npu function: op, object, attributes."""
    op, typ = v["op"], v["typ"] or "?"
    ident = v["ident"]
    if op == "create" and v["created"] and v["status"] == OK:
        target = ""
    elif isinstance(ident, str) and ident.startswith("oid:"):
        target = names.name(ident)
    elif ident:
        target = names.entry(ident)
    else:
        target = ""
    if op in ("create", "set", "flush_fdb_entries"):
        rest = names.attrs(v["attrs"])
    elif op in ("get", "get_stats", "clear_stats", "get_availability"):
        rest = names.attrs(v["attrs"], values=False)
    else:
        rest = ""
    return " ".join(x for x in (op, typ, target, rest) if x)


# ---------------------------------------------------------------- packets

def _ports(ports):
    if not ports and ports != 0:
        return "-"
    if not isinstance(ports, list):
        return str(ports)
    out = []
    for p in ports:
        out.append(str(p[1]) if isinstance(p, list) and len(p) == 2 else str(p))
    return ", ".join(out)


def _port_word(ports):
    text = _ports(ports)
    return ("ports " if "," in text else "port ") + text


def _pkt(p):
    if not isinstance(p, dict):
        return str(p)
    tag = " [MLD]" if p.get("mld") else ""
    text = str(p.get("sum", "?")).replace(" IPv4 ", " ").replace(" IPv6 ", " ")
    text = text.removesuffix(" [S]")
    return f"{p.get('len', '?')}B {text}{tag}"


def _hexdump(hx, limit=64):
    if not hx:
        return []
    raw = hx[: limit * 2]
    lines = []
    for off in range(0, len(raw), 32):
        chunk = raw[off:off + 32]
        lines.append(f"{off // 2:04x}  " + " ".join(chunk[i:i + 2] for i in range(0, len(chunk), 2)))
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


def _failing_report(reports):
    for when in ("setup", "call", "teardown"):
        rep = reports.get(when)
        if rep and (rep.get("outcome") == "failed" or rep.get("wasxfail") is not None) \
                and (rep.get("exc") or rep.get("longrepr")):
            return rep
    return None


# ---------------------------------------------------------------- the run

class Run:
    """Everything a test's log needs from the whole run: header, test bed, aliases, switch inits."""

    def __init__(self, events):
        self.events = events
        self.header = next((e for e in events if e.get("k") == "session"), {}) or {}
        self.testbed = next((e for e in events if e.get("k") == "testbed"), {}) or {}
        self.run_end = next((e for e in events if e.get("k") == "run_end"), None)
        self.collected = next((e.get("n") for e in events if e.get("k") == "collected"), None)
        self.clients = sorted({str(e.get("cls")) for e in events if e.get("k") == "client"})
        self.cache = _run_cache(self.header.get("run"))
        self.enums = dict(learn_enums(events, self.cache))
        self.types = dict(learn_types(events, self.cache))
        self.seen = self.cache["seen"]
        self.npu = {e["id"]: e for e in events if e.get("k") == "npu" and e.get("id") is not None}
        self.order, self.by_test, self.alias_start, self.alias_body = [], {}, {}, {}
        aliases, dvid = {}, None
        for e in events:
            if e.get("k") == "aliases":
                aliases = merge_aliases(aliases, e.get("map"))
                if e.get("default_vlan_id") not in (None, "", "0"):
                    dvid = e.get("default_vlan_id")
            nodeid = e.get("test")
            if not nodeid:
                continue
            if nodeid not in self.by_test:
                self.order.append(nodeid)
                self.by_test[nodeid] = []
                self.alias_start[nodeid] = dict(aliases)
            self.by_test[nodeid].append(e)
            if nodeid not in self.alias_body and e.get("phase") in ("call", "teardown"):
                self.alias_body[nodeid] = (dict(aliases), dvid)
        for nodeid in self.order:
            self.alias_body.setdefault(nodeid, (dict(aliases), dvid))
        self.tests = {}
        for i, nodeid in enumerate(self.order, 1):
            t = next((e for e in self.by_test[nodeid] if e.get("k") == "test"), {}) or {}
            self.tests[nodeid] = {"ev": t, "index": t.get("index") or i}
        self.blocks = self._init_blocks()
        self.in_block = {e.get("seq") for b in self.blocks for e in b["events"]}
        self.block_roots = {b["root"] for b in self.blocks if b["root"] is not None}
        aliases, pending = {}, list(self.blocks)
        for e in events:
            if pending and e.get("seq", 0) > pending[0]["events"][-1].get("seq", 0) and e.get("k") != "aliases":
                pending.pop(0)["aliases"] = dict(aliases)
            if e.get("k") == "aliases":
                aliases = merge_aliases(aliases, e.get("map"))
        for b in pending:
            b["aliases"] = dict(aliases)

    def root(self, npu_id):
        """The top-level npu call an npu call (or a SAI call's npu id) belongs to."""
        seen = 0
        while npu_id is not None and seen < 100:
            parent = (self.npu.get(npu_id) or {}).get("parent")
            if parent is None:
                return npu_id
            npu_id, seen = parent, seen + 1
        return npu_id

    def _init_blocks(self):
        """Switch inits: a client reset ('cleanup') and the calls after it, until the work that did it
        ends (the same npu call, e.g. npu.reset(), or the same test, phase and fixture)."""
        blocks, cur = [], None
        for e in self.events:
            if e.get("k") != "sai":
                continue
            root = self.root(e.get("npu"))
            ctx = (e.get("test"), e.get("phase"), e.get("fx"))
            start = e.get("op") == "cleanup" and not e.get("depth") and e.get("phase") in ("setup", "call")
            if not start and cur is None and not blocks and e.get("fx") == "testbed_instance":
                start = True
            if start:
                cur = {"root": root, "ctx": ctx, "events": [e]}
                blocks.append(cur)
                continue
            if cur is not None:
                if (cur["root"] is not None and root == cur["root"]) or (cur["root"] is None and root is None
                                                                         and ctx == cur["ctx"]):
                    cur["events"].append(e)
                    continue
                cur = None
        good = []
        for b in blocks:
            creates = [sai_view(e) for e in b["events"] if e.get("op") == "create"]
            if any(v["typ"] == "SWITCH" for v in creates):
                first = b["events"][0]
                b.update(test=first.get("test"), phase=first.get("phase"), fx=first.get("fx"), ts=_ts(first),
                         seq=first.get("seq", 0))
                good.append(b)
        return good

    def block_for(self, nodeid):
        """The switch init this test ran on: the last one that started before its test body."""
        events = self.by_test.get(nodeid) or []
        body = next((e for e in events if e.get("phase") == "call"), None) or (events[-1] if events else None)
        limit = (body or {}).get("seq", 0)
        found = None
        for b in self.blocks:
            if b["seq"] < limit:
                found = b
        return found


# ---------------------------------------------------------------- one test

def _teardown_fixtures(events):
    """Which fixture each teardown event belongs to: events since the previous fixture_done marker
    belong to the fixture that just finished. Returns {seq: (name, scope)}."""
    owner, buffer = {}, []
    for e in events:
        if e.get("phase") != "teardown":
            continue
        if e.get("k") == "fixture_done":
            for b in buffer:
                owner[b.get("seq")] = (e.get("name"), e.get("scope"))
            buffer = []
        else:
            buffer.append(e)
    return owner


def _units(run, events):
    """The test's steps in time order: top-level npu calls (with the SAI calls they made), SAI calls
    made outside npu functions, asserts, packets sent and packet checks (with their polls)."""
    owner = _teardown_fixtures(events)
    children = collections.defaultdict(list)
    units, checks, polls = [], [], []
    session_teardown = 0

    def unit(kind, ev, ts, **kw):
        fx, scope = ev.get("fx"), ev.get("fxs")
        if ev.get("phase") == "teardown" and ev.get("seq") in owner:
            fx, scope = owner[ev.get("seq")]
        return dict(k=kind, ev=ev, ts=ts, phase=ev.get("phase"), site=ev.get("site") or {}, fx=fx, scope=scope, **kw)

    for e in events:
        k = e.get("k")
        if k == "sai":
            if e.get("seq") in run.in_block:
                continue
            u = unit("sai", e, _ts(e))
            if u["phase"] == "teardown" and u["scope"] in SESSION_SCOPES:
                session_teardown += 1
                continue
            r = run.root(e.get("npu"))
            if r is None:
                units.append(u)
            else:
                children[r].append(e)
        elif k == "npu" and e.get("parent") is None:
            units.append(unit("npu", e, _ts(e)))
        elif k in ("assert", "send", "note"):
            units.append(unit(k, e, _ts(e)))
        elif k == "pktcheck":
            u = unit(k, e, _ts(e) - float(e.get("dur") or 0), polls=[])
            units.append(u)
            checks.append(u)
        elif k == "poll":
            polls.append(e)
    top = [(u["ev"].get("seq"), sai_view(u["ev"])) for u in units if u["k"] == "sai"]
    probes = _probes(top)
    units = [u for u in units if not (u["k"] == "sai" and u["ev"].get("seq") in probes)]
    out = []
    for u in units:
        if u["k"] == "npu":
            sais = sorted(children.get(u["ev"].get("id"), []), key=lambda x: x.get("seq", 0))
            u["sais"] = sais
            if u["phase"] == "teardown" and u["scope"] in SESSION_SCOPES:
                session_teardown += len(sais)
                continue
            if not sais and not u["ev"].get("exc") and u["ev"].get("id") not in run.block_roots:
                continue        # no SAI call: a pure helper such as npu.fdb_entry_key
        out.append(u)
    for p in polls:
        owner_check = next((c for c in checks if p.get("within") and c["ts"] - 0.001 <= _ts(p) <= _ts(c["ev"]) + 0.001),
                           None)
        if owner_check is not None:
            owner_check["polls"].append(p)
        else:
            out.append(unit("poll", p, _ts(p)))
    out.sort(key=lambda u: (u["ts"], u["ev"].get("seq", 0)))
    # repeats of the same passing assert become one line
    merged = []
    for u in out:
        prev = merged[-1] if merged else None
        if prev and u["k"] == "assert" and prev["k"] == "assert" and \
                (prev["ev"].get("file"), prev["ev"].get("line"), prev["ev"].get("orig")) == \
                (u["ev"].get("file"), u["ev"].get("line"), u["ev"].get("orig")):
            prev["n"] = prev.get("n", 1) + 1
            continue
        merged.append(u)
    return merged, session_teardown


class Ctx:
    """What rendering a test collects: names, counts, checks and objects created or removed."""

    def __init__(self, names, test_file):
        self.names = names
        self.test_file = test_file
        self.sai = 0
        self.checks = []
        self.last_bad = None
        self.created = {}         # okey -> {"name", "typ", "attrs"}
        self.removed = {}         # okey -> {"name", "typ"}
        self.last_sent = None
        self.pkt_miss = None
        self.fail_msg = ""
        self.result = None
        self.alias_events = []

    def at(self, ts):
        """Apply alias updates (switch re-init, recreated bridge ports) up to this time."""
        while self.alias_events and _ts(self.alias_events[0]) <= ts:
            self.names.set_base(self.alias_events.pop(0).get("map"))


def _npu_args(ev, names):
    args = dict(ev.get("args") or {})
    args.pop("do_assert", None)
    fn = ev.get("fn") or "?"
    parts = []
    for i, (k, v) in enumerate(args.items()):
        if k == "args" and isinstance(v, list):          # signature not known: positional values
            parts.extend(names.value(x) for x in v)
            continue
        if k in ("attrs", "attr") and v in ([], None):
            continue
        if isinstance(v, list) and v and isinstance(v[0], str) and "_ATTR_" in v[0]:
            if fn in ("get", "get_stats", "clear_stats", "bulk_get"):
                # names to read; the values next to them are placeholders
                parts.append(", ".join(short_attr(x) for x in v if isinstance(x, str) and "_ATTR_" in x))
            elif len(v) % 2 == 0:
                parts.append(", ".join(f"{short_attr(a)}={names.value(x, a)}" for a, x in _pairs(v)))
            else:
                parts.append(names.value(v))
            continue
        text = names.obj(v) if i == 0 else names.value(v)
        parts.append(text if i < 3 or k in ("obj", "vlan_oid", "bp_oid", "mac") else f"{k}={text}")
    return f"npu.{fn}(" + ", ".join(parts) + ")"


def _sai_note(sais, fn):
    ops = collections.Counter(e.get("op") for e in sais)
    if len(sais) <= 1 or set(ops) == {fn}:
        return ""
    return "  (SAI: " + ", ".join(f"{n} {op}" for op, n in ops.items()) + ")"


def _process_sais(sais, ctx):
    """Count a call's SAI client calls, name what they created, note removes and failures.
    Returns (first status that is not SUCCESS, the views)."""
    views = [(e.get("seq"), sai_view(e)) for e in sais]
    probes = _probes(views)
    bad = None
    for key, v in views:
        ctx.sai += 1
        if v["op"] == "cleanup":
            continue
        status = v["status"]
        if key not in probes and status not in (OK, None) and bad is None:
            bad = status
        if v["op"] == "create" and status == OK:
            ident = v["created"] or v["ident"]
            name = ctx.names.created(v["typ"], v["created"]) if v["created"] else \
                f"{v['typ']}{ctx.names.entry(ident) if ident else ''}"
            ctx.created[_okey(ident)] = {"name": name, "typ": v["typ"], "attrs": v["attrs"]}
        elif v["op"] == "remove" and status == OK and v["ident"]:
            key2 = _okey(v["ident"])
            typ = v["typ"] or ctx.names.types.get(v["ident"])
            if key2 not in ctx.removed:
                ident = v["ident"]
                name = ctx.names.name(ident) if str(ident).startswith("oid:") else f"{typ}{ctx.names.entry(ident)}"
                if name.startswith("oid:"):
                    name = f"{typ} {name}"
                ctx.removed[key2] = {"name": name, "typ": typ}
    return bad, [v for _, v in views]


def _npu_row(u, ctx, run):
    ev = u["ev"]
    names = ctx.names
    left = _npu_args(ev, names)
    bad, _ = _process_sais(u.get("sais") or [], ctx)
    ret, exc = ev.get("ret"), ev.get("exc")
    fn = ev.get("fn")
    if exc:
        right = f"-> raised {exc.get('type')}: {_first_line(exc.get('msg'), 100)}"
    else:
        status, value = None, ret
        if isinstance(ret, list) and len(ret) == 2 and isinstance(ret[0], str) and ret[0].startswith("SAI_STATUS"):
            status, value = ret
        elif isinstance(ret, str) and ret.startswith("SAI_STATUS"):
            status, value = ret, None
        status = status if status not in (None, OK) else (bad or status)
        if status and status != OK:
            right = f"-> status {names.enum(status)}"
        elif isinstance(value, str) and value.startswith("oid:"):
            right = "-> " + names.name(value, first_use=True)
        elif isinstance(value, dict) and "_saidata" in value:
            pairs = _pairs(value["_saidata"])
            right = "-> " + (names.value(pairs[0][1], pairs[0][0]) if len(pairs) == 1 else names.value(value))
        elif isinstance(value, dict) or value in (None, "", []) or status == OK:
            # a create of a keyed object (FDB entry, route) returns its key: nothing new to show
            right = "-> ok"
        else:
            right = "-> " + names.value(value)
    if ev.get("id") in run.block_roots:
        right += "  (switch reset and initialised again: see switch init in SETUP)"
    else:
        right += _sai_note(u.get("sais") or [], fn)
    if (bad and bad != OK) or exc:
        ctx.last_bad = f"{left} {right}"
    return left, right


def _sai_row(u, ctx):
    _, views = _process_sais([u["ev"]], ctx)
    v = views[0]
    if v["op"] == "cleanup":
        return "SAI client cleanup (switch reset)", None
    left = _sai_text(v, ctx.names)
    if v["exc"]:
        right = f"-> raised {v['exc'].get('type')}: {_first_line(v['exc'].get('msg'), 100)}"
    elif v["status"] not in (OK, None):
        right = f"-> status {ctx.names.enum(v['status'])}"
    elif v["op"] == "create" and v["created"]:
        right = "-> " + ctx.names.name(v["created"], first_use=True)
    elif v["read"]:
        right = "-> " + ", ".join(f"{short_attr(k)}={ctx.names.value(x, k)}" for k, x in v["read"])
    else:
        right = "-> ok" if v["status"] == OK else "-> (no status returned by the client)"
    if v["status"] not in (OK, None) or v["exc"]:
        ctx.last_bad = f"{left} {right}"
    return left, right


def _check_where(ev, ctx):
    site = ev.get("site") or {}
    if site.get("line"):
        return f"{site.get('file') or ctx.test_file}:{site['line']}"
    return "ptf"


def _assert_row(u, ctx):
    ev = u["ev"]
    n = u.get("n", 1)
    file = _base(ev.get("file"))
    expl = _first_line(ev.get("expl"), 80)
    res = "PASS" if n == 1 else f"PASS x{n}"
    what = f"assert {_short(ev.get('orig'), 90)}"
    ctx.checks.append({"res": res, "where": f"{file}:{ev.get('line')}", "what": what, "val": expl})
    return what, f"{res}  {expl}"


def _pkt_desc(ev):
    func = ev.get("func") or "?"
    ports = ev.get("ports")
    if not ev.get("ok") and not func.startswith(("verify_no", "count_matched")):
        return f"expected on {_ports(ports)}" + (", nothing elsewhere" if func == "verify_packets" else "")
    if func.startswith("verify_no_other"):
        return "nothing on any other port"
    if func.startswith("verify_no"):
        return f"nothing on {_port_word(ports)}" if ports is not None else "nothing on any port"
    if func == "verify_packets":
        return f"arrived on {_ports(ports)}, nothing elsewhere"
    if func.startswith("count_matched"):
        return f"matched {ev.get('count')}" if ev.get("count") is not None else "counted"
    return f"arrived on {_ports(ports)}" if ports is not None else "arrived"


def _pktcheck_rows(u, ctx, prefix, cont):
    ev = u["ev"]
    func = ev.get("func") or "?"
    ports = ev.get("ports")
    left = f"{func}({_port_word(ports)})" if ports is not None else f"{func}()"
    if ev.get("ok"):
        res = "PASS"
    elif u.get("fail_label"):
        res = u["fail_label"]
    elif ctx.result == "Pass" or not (ctx.fail_msg and ctx.fail_msg in str(ev.get("msg") or "")):
        res = "MISS (caught by the test)"
    else:
        res = "FAIL"
    rows = [(prefix + left, f"{res}  {_pkt_desc(ev)}")]
    exp = ev.get("exp")
    if exp and (not ctx.last_sent or exp.get("hex") != ctx.last_sent.get("hex")):
        rows.append((f"{cont}expected  {_pkt(exp)}", None))
    if not ev.get("ok"):
        for p in u.get("polls") or []:
            if p.get("ok"):
                if str(p.get("within") or "").startswith("verify_no"):
                    got = p.get("got") or {}
                    rows.append((f"{cont}got on port {got.get('port')}  {_pkt(got)}", None))
                continue
            where = f"port {p.get('port')}" if p.get("port") is not None else "any port"
            if p.get("exp") is None:
                continue
            rows.append((f"{cont}nothing matching on {where} within {p.get('timeout')} s", None))
            ctx.pkt_miss = {"where": where, "timeout": p.get("timeout"), "recent": p.get("recent") or []}
            for seen in p.get("recent") or []:
                rows.append((f"{cont}seen instead  {_pkt(seen)}", None))
                if res.startswith(("FAIL", "ERROR")):
                    rows.extend((f"{cont}  {h}", None) for h in _hexdump(seen.get("hex")))
    where = _check_where(ev, ctx)
    ctx.checks.append({"res": res, "where": where, "what": left, "val": "", "func": func, "kind": "pkt"})
    return rows


def _unit_rows(u, ctx, run, prefix, cont):
    """Rows for one step. prefix starts the first line; cont is the indent of continuation lines."""
    k = u["k"]
    ctx.at(u["ts"])
    if k == "npu":
        left, right = _npu_row(u, ctx, run)
        return [(prefix + left, right)]
    if k == "sai":
        left, right = _sai_row(u, ctx)
        return [(prefix + left, right)]
    if k == "assert":
        left, right = _assert_row(u, ctx)
        return [(prefix + left, right)]
    if k == "send":
        ev = u["ev"]
        ctx.last_sent = ev
        return [(f"{prefix}send_packet(port {ev.get('port')})", None), (f"{cont}frame     {_pkt(ev)}", None)]
    if k == "pktcheck":
        return _pktcheck_rows(u, ctx, prefix, cont)
    if k == "poll":
        ev = u["ev"]
        if ev.get("ok"):
            got = ev.get("got") or {}
            return [(f"{prefix}poll", f"got on port {got.get('port')}  {_pkt(got)}")]
        return [(f"{prefix}poll port {ev.get('port')}", f"nothing within {ev.get('timeout')} s")]
    if k == "note":
        return [(f"{prefix}note: {u['ev'].get('text')}", None)]
    if k == "fail":
        ctx.checks.append({"res": u["label"], "where": u["where"], "what": u["src"], "val": u["msg"]})
        return [(prefix + u["src"], f"{u['label']}  {u['msg']}")]
    return []


def _helpers(u, skip=None):
    """The lib helpers a step came from, without lambdas and comprehensions (wrappers, not functions
    anyone calls by name) and without the fixture already named in the title."""
    return [h for h in (u["site"].get("helpers") or [])
            if not str(h.get("fn") or "").rsplit(".", 1)[-1].startswith("<")
            and not (skip and _is_fixture_helper(h, skip))]


def _helper_key(u, skip=None):
    hs = _helpers(u, skip)
    if not hs or u["k"] in ("send", "pktcheck", "poll", "fail"):
        return None
    h = hs[-1]
    return (h.get("fid") or h.get("fn"), bool(h.get("exit")), h.get("fn"),
            json.dumps(h.get("args"), sort_keys=True, default=str))


def _is_fixture_helper(h, fixture):
    fn = str(h.get("fn") or "")
    return fn == fixture or fn.endswith("." + fixture)


def _helper_title(u, ctx, skip=None):
    h = _helpers(u, skip)[-1]
    args = ", ".join(ctx.names.value(v) for k, v in (h.get("args") or []) if k != "do_assert")
    return _short(f"{h.get('fn')}({args})", 110)


def _shape(u, rows):
    """Steps with the same shape (same function or SAI op, object type and attributes, and a good result)
    can be folded when many come in a row; a step that did not succeed never is."""
    if len(rows) != 1 or rows[0][1] is None or not rows[0][1].startswith("-> ") or \
            rows[0][1].startswith(("-> status", "-> raised", "-> (no status")):
        return None
    ev = u["ev"]
    if u["k"] == "npu":
        return ("npu", ev.get("fn"), tuple((ev.get("args") or {}).keys()))
    if u["k"] == "sai":
        v = sai_view(ev)
        return ("sai", v["op"], v["typ"], tuple(k for k, _ in v["attrs"]))
    return None


def _steps_rows(units, render, pad, fold=5):
    """Rows for consecutive steps; a run of `fold` or more steps of the same shape keeps the first two and
    the last, with a count in between (every call is still in the .rec)."""
    done = [(u, render(u)) for u in units]          # render all: names and counts need every step
    out, i = [], 0
    while i < len(done):
        shape = _shape(*done[i])
        j = i + 1
        while shape is not None and j < len(done) and _shape(*done[j]) == shape:
            j += 1
        if shape is not None and j - i >= fold:
            out += done[i][1] + done[i + 1][1]
            out.append((" " * pad + f"… {j - i - 3} more like this (same call, other objects; all in the .rec) …", None))
            out += done[j - 1][1]
        else:
            for _, rows in done[i:j]:
                out += rows
        i = j
    return out


def _group_rows(units, ctx, run, clock=True, undo=False, indent="  ", skip=None):
    """Rows for a list of steps, grouped under the lib helper that made them (consecutive steps of
    the same helper call). Entering helpers get a title line; exiting ones a 'name exit' column."""
    rows = []
    stamp = (lambda u: _clock(u["ts"]) + "  ") if clock else (lambda u: "")
    blank = " " * (14 if clock else 0)
    groups = [(key, list(g)) for key, g in itertools.groupby(units, key=lambda u: _helper_key(u, skip))]
    title_w = max((len(f"{key[2]} exit") for key, _ in groups if key), default=0) if undo else 0
    for key, g in groups:
        if key is None:
            rows.extend(_steps_rows(g, lambda u: _unit_rows(u, ctx, run, indent + stamp(u), indent + blank + "  "),
                                    len(indent + blank) + (2 if clock else 0)))
            continue
        if undo:
            title = f"{key[2]} exit".ljust(title_w)
            for i, u in enumerate(g):
                first = indent + stamp(u) + title + "  " if i == 0 else indent + blank + " " * (title_w + 2)
                rows.extend(_unit_rows(u, ctx, run, first, indent + blank + " " * (title_w + 4)))
            continue
        ctx.at(g[0]["ts"])
        rows.append((indent + stamp(g[0]) + _helper_title(g[0], ctx, skip), None))
        rows.extend(_steps_rows(g, lambda u: _unit_rows(u, ctx, run, indent + blank + "  ", indent + blank + "    "),
                                len(indent + blank) + 2))
    return rows


def _mode(u):
    if u["k"] == "fail":
        return "body"
    hs = u["site"].get("helpers") or []
    if any(h.get("exit") for h in hs):
        return "undo"
    if any(h.get("gen") for h in hs):
        return "configure"
    return "body"


def _src(text):
    text = str(text or "").strip().rstrip("\\").rstrip()
    more = text.endswith(",")
    text = text.rstrip(":").rstrip(",").rstrip()
    return _short(text + (", ..." if more else ""), 90)


def _section_title(mode, units):
    lines = sorted({u["site"].get("line") for u in units if u["site"].get("line")})
    src = next((u["site"].get("src") for u in units if u["site"].get("line") == (lines[0] if lines else None)), "")
    # Python 3.13 reports each item of a multi-line with statement on its own line (3.12: the with line)
    where = f"line {lines[0]}" if len(lines) == 1 else (f"lines {lines[0]}-{lines[-1]}" if lines else "")
    is_with = str(src).startswith("with ")
    if mode == "undo":
        if is_with:
            return f"Undo (leaving the with block on {where})"
        return "Undo" + (f" ({where})" if where else "")
    if mode == "configure" and is_with and len(lines) > 1:
        return f"Configure ({where}: {_src(src)})"
    if mode == "configure":
        word = "Configure"
    elif any(u["k"] in ("send", "pktcheck", "poll") for u in units):
        word = "Send and check"
    elif any(u["k"] in ("npu", "sai") for u in units):
        word = "Test steps"
    else:
        word = "Check"
    if len(lines) == 1:
        return f"{word} (line {lines[0]}: {_src(src)})"
    return word + (f" (lines {lines[0]}-{lines[-1]})" if lines else "")


def _body_lines(units, ctx, run):
    if not units:
        return ["  (nothing recorded)"]
    out = []
    sections = [(m, list(g)) for m, g in itertools.groupby(units, key=_mode)]
    for i, (mode, sec) in enumerate(sections):
        if i:
            out.append("")
        out.append(_section_title(mode, sec))
        out.extend(_align(_group_rows(sec, ctx, run, clock=True, undo=(mode == "undo"))))
    return out


def _fixture_lines(units, ctx, run):
    """SETUP or CLEANUP work of function fixtures, grouped by fixture (no timestamps)."""
    out = []
    for fx, g in itertools.groupby(units, key=lambda u: u["fx"] or "(no fixture)"):
        g = list(g)
        rows = [(f"  fixture {fx}", None)]
        rows.extend(_group_rows(g, ctx, run, clock=False, undo=False, indent="    ", skip=fx))
        out.extend(_align(rows))
    return out


def _init_lines(block, run, this_test):
    """The switch init a test ran on (option B: in full, in every file), grouped by op and type."""
    if block is None:
        return ["  switch init      not recorded in this run's events"]
    where_test = block.get("test")
    info = run.tests.get(where_test) or {}
    phase = {"setup": "setup", "call": "test body", "teardown": "cleanup"}.get(block.get("phase"), block.get("phase"))
    if where_test == this_test:
        where = f"in this test's {phase}"
    elif where_test:
        where = f"in the {phase} of test {info.get('index')}, {(info.get('ev') or {}).get('name') or where_test}"
    else:
        where = "before the first test"
    first = block is run.blocks[0]
    why = ""
    if not first:
        if block.get("fx") == "on_prev_test_failure":
            why = ", after the previous test failed"
        elif block.get("root") is not None:
            why = ", by npu.reset()"
    head = f"once for the run, at {_clock(block['ts'])}, {where}:" if first else \
        f"redone at {_clock(block['ts'])}, {where}{why}:"
    lines = [f"  {'switch init':<{LABEL_W}}{head}"]
    key = (block["seq"], len(block["events"]))
    if key not in run.cache["init"]:
        run.cache["init"][key] = _init_rows(block, run)
    return lines + run.cache["init"][key]


def _init_rows(block, run):
    names = Names(block.get("aliases"), dict(run.enums))
    names.types.update(run.types)
    views = [(e.get("seq"), sai_view(e)) for e in block["events"]]
    probes = _probes(views)
    groups, order = {}, []
    gets, bad = [], []
    for key, v in views:
        if key in probes:
            continue
        if v["status"] not in (OK, None) and v["op"] != "cleanup":
            bad.append(v)
            continue
        if v["op"] == "get":
            if "get" not in groups:
                groups["get"] = gets
                order.append("get")
            gets.append(v)
            continue
        typ = v["typ"] or (names.types.get(v["ident"]) if isinstance(v["ident"], str) else None)
        g = (v["op"], None if v["op"] in ("flush_fdb_entries", "cleanup") else (typ or "(type not sent)"))
        if g not in groups:
            groups[g] = []
            order.append(g)
        groups[g].append(v)
    rows = []
    labels = [f"{op} {typ}" if typ else op for op, typ in (g for g in order if g != "get")]
    label_w = min(max([15] + [len(x) for x in labels]), 24)
    for g in order:
        if g == "get":
            per = collections.OrderedDict()
            for v in gets:
                typ = v["typ"] or names.types.get(v["ident"]) or "?"
                per.setdefault(typ, collections.Counter()).update(short_attr(k) for k, _ in v["attrs"])
            label = f"{len(gets)} gets"
            label_w2 = max(len(t) for t in per) + 2 if per else 2
            first_row = True
            for typ, counter in per.items():
                words = [a if n == 1 else f"{a} x{n}" for a, n in counter.items()]
                words = [w + "," for w in words[:-1]] + words[-1:]
                pre = f"    {(label if first_row else ''):<{label_w}}  {typ:<{label_w2}}"
                rest = " " * len(pre)
                rows.extend(_wrap(words, 104, pre, rest))
                first_row = False
            if probes:
                rows.append(f"    {'size probes':<{label_w}}  {len(probes)} (a list get answered BUFFER_OVERFLOW with the "
                            "size, then repeated)")
            continue
        op, typ = g
        vs = groups[g]
        if op == "cleanup":
            rows.append(f"    {'reset':<{label_w}}  SAI client cleanup, target state flushed")
            continue
        for v in vs:
            if v["op"] == "create" and v["created"]:
                names.types[v["created"]] = v["typ"]
        label = f"{op} {typ}" if typ else op
        if typ == "(type not sent)":
            src = {run.seen.get(v["ident"]) for v in vs}
            if len(src) == 1 and None not in src:
                label = f"{op} ?"
                typ_note = f"(type not sent; these OIDs were read from {src.pop()})"
            else:
                typ_note = None
        else:
            typ_note = None
        if len(vs) == 1:
            v = vs[0]
            words = [f"{short_attr(k)}={names.value(x, k)}" for k, x in v["attrs"]]
            if op != "create" and v["ident"]:
                words.insert(0, names.obj(v["ident"]))
            if op == "create" and v["created"]:
                words.append(f"-> {v['created']}")
        else:
            words = [f"x{len(vs)},"]
            keys = []
            for v in vs:
                for k, _ in v["attrs"]:
                    if k not in keys:
                        keys.append(k)
            for k in keys:
                vals = [names.value(dict(v["attrs"]).get(k), k) for v in vs if k in dict(v["attrs"])]
                if len(set(vals)) == 1:
                    words.append(f"{short_attr(k)}={vals[0]},")
                else:
                    words.append(f"{short_attr(k)}={vals[0]}..{vals[-1]},")
            words[-1] = words[-1].rstrip(",")
        if typ_note:
            words.append(typ_note)
        pre = f"    {label:<{label_w}}  "
        rows.extend(_wrap(words, 104, pre, " " * len(pre)))
    for v in bad:
        rows.append(f"    {'not SUCCESS':<{label_w}}  {_sai_text(v, names)} -> status {names.enum(v['status'])}")
    return rows


def _testbed_lines(run, nodeid):
    h, tb = run.header, run.testbed
    aliases, dvid = run.alias_body.get(nodeid, ({}, None))
    by_name = {v: k for k, v in aliases.items()}
    rows = []

    def row(label, value):
        if value not in (None, ""):
            rows.append(f"  {label:<{LABEL_W}}{value}")

    profile = h.get("profile")
    row("profile", f"{profile}  (tests/poc/targets/{profile}.json)" if profile else "(none)")
    testbed = tb.get("name") or h.get("testbed")
    row("testbed file", f"testbeds/{testbed}.json" if testbed else None)
    if tb.get("target") or tb.get("asic"):
        row("SAI target", f"{tb.get('target')}  (asic={tb.get('asic')}"
                          + (f", sku={tb.get('sku')}" if tb.get("sku") else "") + ")")
    client = tb.get("client") or (run.clients[0] if run.clients else None)
    if client:
        path = CLIENT_PATH.get(client)
        row("SAI client", f"{client} -> {path}" if path else f"{client} ({tb.get('client_type') or '?'} client)")
    traffic = tb.get("run_traffic")
    if traffic is None:
        traffic = h.get("traffic")
    if traffic:
        row("traffic mode", "True  (packets are really sent with PTF)")
    elif h.get("traffic"):
        row("traffic mode", "False  (control plane only: this target does not forward; traffic tests skip)")
    else:
        row("traffic mode", "False  (control plane only: run without --traffic; traffic tests skip)")
    row("switch oid", by_name.get("switch"))
    ports = sorted(((int(re.search(r"\d+", n).group()), o) for n, o in by_name.items() if n.startswith("port[")))
    if ports:
        shown = ", ".join(f"[{i}]={o}" for i, o in ports[:3])
        row("switch ports", f"{len(ports)}  {shown}" + (" ..." if len(ports) > 3 else ""))
    if by_name.get("default_vlan"):
        row("default VLAN", f"id={dvid or '?'}  {by_name['default_vlan']}")
    if h.get("ports") is not None:
        row("profile ports", str(h.get("ports")))
    mld = f"MLD filter {'on' if h.get('mld') else 'off'}"
    if h.get("filters") is not None:
        row("traffic filters", f"{h['filters']}  ({mld})")
    elif h.get("mld") is not None:
        row("traffic filters", mld)
    dp = tb.get("dataplane")
    if dp:
        dports = dp.get("ports") or []
        shown = ", ".join(f"{a}={n}" for a, n in dports[:4])
        row("dataplane", f"{dp.get('type')} ({dp.get('mode')}), {len(dports)} ports: {shown}"
                         + (" ..." if len(dports) > 4 else ""))
    return rows


def _objects_lines(ctx):
    """CLEANUP summary: objects the test created and removed, objects that existed before and were
    removed and re-created, and anything left behind."""
    created, removed = ctx.created, ctx.removed
    gone = [k for k in created if k in removed]
    left = [k for k in created if k not in removed]
    pre = [k for k in removed if k not in created]
    restored, removed_only = [], []
    for k in pre:
        typ = removed[k]["typ"]
        new = next((x for x in left if created[x]["typ"] == typ), None)
        if new is None:
            removed_only.append(removed[k]["name"])
        else:
            left.remove(new)
            restored.append(new)
    rows = []

    def row(label, n, items):
        rows.append(f"  {label:<21}{n:<4}{items}".rstrip())

    row("created and removed", len(gone), _compact([created[k]["name"] for k in gone], [created[k]["typ"] for k in gone]))
    if restored:
        descr = [_describe(created[k], ctx.names) for k in restored]
        row("restored", len(restored), ", ".join(descr))
        rows.append(f"  {'':<25}(existed before the test; removed and re-created, so the OIDs are new)")
    if removed_only:
        row("removed, existed", len(removed_only), ", ".join(removed_only))
    row("left behind", len(left), _compact([created[k]["name"] for k in left], [created[k]["typ"] for k in left]))
    return rows


def _compact(names, types):
    """VLAN#1, VLAN_MEMBER#1-#3, FDB_ENTRY x3."""
    by_type = collections.OrderedDict()
    for n, t in zip(names, types):
        by_type.setdefault(t, []).append(n)
    parts = []
    for t, ns in by_type.items():
        nums = []
        for n in ns:
            m = re.fullmatch(rf"{re.escape(str(t))}#(\d+)", str(n))
            nums.append(int(m.group(1)) if m else None)
        if all(x is not None for x in nums) and nums:
            nums.sort()
            if len(nums) > 2 and nums == list(range(nums[0], nums[-1] + 1)):
                parts.append(f"{t}#{nums[0]}-#{nums[-1]}")
            else:
                parts.extend(f"{t}#{x}" for x in nums)
        elif len(ns) > 2:
            parts.append(f"{t} x{len(ns)}")
        else:
            parts.extend(ns)
    return ", ".join(parts)


def _describe(obj, names):
    """A re-created object by the objects it points to: VLAN_MEMBER(default_vlan, bp[2])."""
    refs = [names.value(v, k) for k, v in obj["attrs"] if isinstance(v, str) and v.startswith("oid:")]
    return f"{obj['typ']}(" + ", ".join(refs) + ")" if refs else obj["name"]


def _why_lines(info, ctx):
    res = info["result"]
    if res == "Pass":
        return []
    t = info["test"]
    rows = []
    xf = t.get("xfail") or {}
    if res in ("Expected gap", "Gap now passes") and xf.get("reason"):
        rows.append(("profile says", f"{xf['reason']}" + (" (strict)" if xf.get("strict") else "")))
    if res == "Gap now passes":
        rows.append(("note", "the test passed although the profile lists it as an expected gap (strict xfail)"))
    if res == "Skipped":
        rep = info["reports"].get("call") or info["reports"].get("setup") or {}
        rows.append(("reason", _skip_reason(rep)))
    if res == "Interrupted":
        rows.append(("note", info["detail"]))
    rep = _failing_report(info["reports"])
    if rep is not None and res not in ("Pass", "Skipped"):
        where = rep.get("where") or {}
        exc = rep.get("exc") or {}
        if where:
            rows.append(("where", f"{_base(where.get('file'))}:{where.get('line')}  {_short(where.get('src'), 90)}"))
        msg = [ln for ln in str(exc.get("msg") or "").splitlines() if ln.strip()][:4]
        if msg:
            rows.append(("got", f"{exc.get('type')}: {_short(msg[0], 120)}"))
            rows.extend(("", _short(ln, 120)) for ln in msg[1:])
        elif isinstance(rep.get("longrepr"), str):
            rows.append(("got", _first_line(rep["longrepr"], 120)))
    if ctx.last_bad and res not in ("Pass", "Skipped", "Gap now passes"):
        rows.append(("last failed call", _short(ctx.last_bad, 150)))
    miss = ctx.pkt_miss
    if miss and res in ("Fail", "Expected gap"):
        seen = "; ".join(_pkt(p) for p in miss["recent"]) or "nothing"
        rows.append(("on " + miss["where"], f"within {miss['timeout']} s, instead of the expected frame: {seen}"))
    if info["detail"] and res != "Interrupted":
        rows.append(("note", info["detail"]))
    return [WHY_TITLE.get(res, res.upper())] + [f"  {label:<{LABEL_W}}{text}" for label, text in rows]


def _checks_lines(ctx):
    if not ctx.checks:
        return ["  (no assert or packet check ran)"]
    rw = max(len(c["res"]) for c in ctx.checks)
    ww = max(len(c["where"]) for c in ctx.checks)
    rows = [(f"  {c['res']:<{rw}}  {c['where']:<{ww}}  {c['what']}", c["val"] or None) for c in ctx.checks]
    return _align(rows, cap=100)


def _fail_unit(info, units, test_file):
    """The failing check from pytest's report (the pass hook sees only passing asserts), placed after
    the last step before the undo that followed it. None when a packet check already shows it."""
    rep = _failing_report(info["reports"])
    if rep is None or info["result"] not in ("Fail", "Expected gap", "Setup error", "Cleanup error"):
        return None
    exc = rep.get("exc") or {}
    where = rep.get("where") or {}
    if not where:
        return None
    src = _short(where.get("src") or "", 90)
    file = _base(where.get("file"))
    etype = exc.get("type")
    label = "FAIL" if etype in (None, "AssertionError") else f"ERROR ({etype})"
    if info["result"] == "Expected gap":
        label += " (expected gap)"
    msg = _first_line(exc.get("msg"), 100)
    phase = rep.get("when")
    for u in reversed(units):
        if u["k"] == "pktcheck" and u["phase"] == phase and not u["ev"].get("ok") \
                and (u["ev"].get("func") or "?") in src:
            u["fail_label"] = label          # the failing packet check is this line's failure: one row
            return None
    fail = {"k": "fail", "ev": rep, "ts": _ts(rep), "phase": phase, "site": {}, "fx": None, "scope": None,
            "label": label, "where": f"{file}:{where.get('line')}", "src": src, "msg": msg}
    if file != test_file:
        # inside a lib helper: the step that raised already shows it; the check goes in CHECKS only
        fail["row"] = False
        return fail
    # on the test line, after the steps before it (and before any undo or finally that ran after it)
    same = [u for u in units if u["phase"] == phase and _mode(u) != "undo"]
    before = [u for u in same if (u["site"].get("line") or 0) <= (where.get("line") or 0)] or same
    fail["ts"] = (before[-1]["ts"] + 1e-6) if before else _ts(rep)
    fail["site"] = {"line": where.get("line"), "src": where.get("src"), "file": file}
    return fail


def _test_info(run, nodeid, finished):
    events = run.by_test.get(nodeid) or []
    t = run.tests.get(nodeid, {}).get("ev") or {}
    reports = {e.get("when"): e for e in events if e.get("k") == "report"}
    result, detail = outcome(t, reports)
    if not finished and not any(e.get("k") == "test_end" for e in events):
        # the run stopped inside this test (Ctrl-C ends pytest at once: the PTF dataplane restores
        # the default SIGINT action), so the result pytest would have given is unknown
        done = [f"{w} {reports[w].get('outcome')}" for w in ("setup", "call", "teardown") if w in reports]
        result = "Interrupted"
        detail = "the run stopped before this test finished" + (f" (done: {', '.join(done)})" if done else "")
    duration = sum(float(r.get("dur") or 0) for r in reports.values())
    return {"test": t, "reports": reports, "result": result, "detail": detail, "duration": duration,
            "events": events}


def render_one(run, nodeid, finished=False):
    """The plain-text log of one test. Returns (text, info)."""
    info = _test_info(run, nodeid, finished)
    t = info["test"]
    events = info["events"]
    names = Names(run.alias_start.get(nodeid), dict(run.enums))
    names.types.update(run.types)
    names.learn_switch_values(run.seen)
    ctx = Ctx(names, _base(t.get("file")))
    ctx.result = info["result"]
    failing = _failing_report(info["reports"])
    ctx.fail_msg = _first_line(((failing or {}).get("exc") or {}).get("msg")) if failing else ""
    units, session_teardown = _units(run, events)
    ctx.alias_events = sorted((e for e in events if e.get("k") == "aliases"), key=_ts)
    fail = _fail_unit(info, units, ctx.test_file)

    def phase_units(phase):
        return [u for u in units if u["phase"] == phase]

    # SETUP
    setup = ["SETUP"]
    setup.extend(_init_lines(run.block_for(nodeid), run, nodeid))
    pcap_start = next((e for e in events if e.get("k") == "pcap_start"), None)
    if pcap_start:
        setup.append(f"  {'pcap capture':<{LABEL_W}}sniffer started on {', '.join(pcap_start.get('ifaces') or [])}")
    su = phase_units("setup")
    if su:
        setup.extend(_fixture_lines(su, ctx, run))
    if fail and fail["phase"] == "setup" and fail.get("row", True):
        setup.extend(_align(_unit_rows(fail, ctx, run, "  ", "    ")))
    rep = info["reports"].get("setup")
    setup.append(f"  setup: {(rep or {}).get('outcome', 'not reported').upper()}"
                 + (f"  ({_skip_reason(rep)})" if rep and rep.get("outcome") == "skipped" else ""))

    # TEST BODY
    body_units = phase_units("call")
    if fail and fail["phase"] == "call" and fail.get("row", True):
        body_units = sorted(body_units + [fail], key=lambda u: u["ts"])
    call = info["reports"].get("call") or {}
    if not body_units and call.get("outcome") == "skipped":
        body = ["TEST BODY", "", f"  (skipped: {_skip_reason(call)})"]
    elif "call" in info["reports"] or body_units:
        body = ["TEST BODY", ""] + _body_lines(body_units, ctx, run)
    else:
        body = ["TEST BODY", "", "  (did not run)"]

    # CLEANUP
    cleanup = ["CLEANUP"]
    tu = phase_units("teardown")
    if tu:
        cleanup.extend(_fixture_lines(tu, ctx, run))
    if fail and fail["phase"] == "teardown" and fail.get("row", True):
        cleanup.extend(_align(_unit_rows(fail, ctx, run, "  ", "    ")))
    if fail and not fail.get("row", True):
        ctx.checks.append({"res": fail["label"], "where": fail["where"], "what": fail["src"], "val": fail["msg"]})
    cleanup.extend(_objects_lines(ctx))
    for p in (e for e in events if e.get("k") == "pcap"):
        cleanup.append(f"  {'pcap':<21}{_base(p.get('path'))} ({p.get('frames')} frames, in this folder)")
    out = next((r.get("stdout") for r in (info["reports"].get(w) for w in ("teardown", "call", "setup"))
                if r and r.get("stdout")), "") or ""
    printed = [ln for ln in out.splitlines() if ln.strip() and not ln.startswith("pcap written:")
               and not ln.startswith("-" * 10)]
    for i, ln in enumerate(printed[:12]):
        cleanup.append(f"  {('printed' if i == 0 else ''):<21}{_short(ln, 100)}")
    if len(printed) > 12:
        cleanup.append(f"  {'':<21}… {len(printed) - 12} more lines (in the pytest-html report)")
    if session_teardown:
        cleanup.append(f"  {'end of run':<21}session teardown after this test: {session_teardown} SAI calls (in the .rec)")
    rep = info["reports"].get("teardown")
    cleanup.append(f"  teardown: {(rep or {}).get('outcome', 'not reported').upper()}")

    # header (needs the counts collected above)
    n_pass = sum(1 for c in ctx.checks if c["res"].startswith("PASS"))
    n_other = len(ctx.checks) - n_pass
    summary = [_plural(n_pass, "check") + " passed"]
    if n_other:
        summary.append(f"{n_other} not passed")
    summary.append(f"{ctx.sai} SAI calls")
    summary.append(f"{info['duration']:.1f} s")
    total = run.collected or len(run.order)
    head = [f"{'TEST':<9}{t.get('name') or nodeid}",
            f"{'FILE':<9}{_rel(t.get('file'))}:{t.get('line')}" if t.get("file") else f"{'FILE':<9}{nodeid}",
            (f"{'RUN':<9}{run.header.get('run')}, test {run.tests.get(nodeid, {}).get('index')} of {total}, "
             f"commit {run.header.get('commit') or 'unknown'}"),
            f"{'RESULT':<9}{info['result']}  ({', '.join(summary)})"]
    if t.get("params"):
        head.append(f"{'PARAMS':<9}" + ", ".join(f"{k}={v}" for k, v in t["params"].items()))
    if t.get("markers"):
        head.append(f"{'MARKERS':<9}" + ", ".join(t["markers"]))
    doc = [ln.strip() for ln in str(t.get("doc") or "").splitlines() if ln.strip()]
    if not doc:
        mdoc = str(t.get("mdoc") or "").strip()
        doc = [f"(no docstring) File: {mdoc}" if mdoc else "(no docstring)"]

    out = head + [""] + doc + [""]
    why = _why_lines(info, ctx)
    if why:
        out += why + [""]
    out += ["TEST BED"] + _testbed_lines(run, nodeid) + [""]
    out += setup + [""] + body + [""] + cleanup + [""]
    out += ["CHECKS"] + _checks_lines(ctx)
    info["n_checks"] = len(ctx.checks)
    info["n_sai"] = ctx.sai
    return "\n".join(out) + "\n", info


def render_test(events, nodeid=None, finished=False):
    """Plain-text log of one test (the pytest-html block). events: the whole run so far; nodeid
    defaults to the last test; finished=True when the test's end event is not written yet."""
    run = Run(events)
    if not run.order:
        return "(no test events)\n"
    return render_one(run, nodeid if nodeid in run.by_test else run.order[-1], finished)[0]


def _why(info):
    res = info["result"]
    if res == "Pass":
        return ""
    if res == "Interrupted":
        return info["detail"]
    if res == "Skipped":
        rep = info["reports"].get("call") or info["reports"].get("setup") or {}
        return _skip_reason(rep)
    rep = _failing_report(info["reports"]) or {}
    msg = _first_line((rep.get("exc") or {}).get("msg") or rep.get("longrepr"), 100)
    if res == "Expected gap":
        reason = (info["test"].get("xfail") or {}).get("reason")
        return _short(reason, 100) if reason else msg
    return msg


def render_index(run, infos):
    h = run.header
    counts = collections.Counter(i["result"] for _, i, _ in infos)
    lines = [f"{'RUN':<10}{h.get('run')}",
             f"{'STARTED':<10}{h.get('start')}",
             f"{'COMMIT':<10}{h.get('commit') or 'unknown'}",
             (f"{'PROFILE':<10}{h.get('profile')}, testbed {h.get('testbed')}, "
              f"traffic {'on' if h.get('traffic') else 'off'}, MLD filter {'on' if h.get('mld') else 'off'}, "
              f"{h.get('gaps')} expected gaps"),
             (f"{'VERSIONS':<10}python {h.get('python')}, pytest {h.get('pytest')}, "
              f"pytest-html {h.get('pytest_html')}, ptf {h.get('ptf')}, scapy {h.get('scapy')}"),
             f"{'SAI LIB':<10}saichallenger from {h.get('saichallenger')}",
             f"{'RESULTS':<10}" + ", ".join(f"{r} {counts[r]}" for r in RESULT_ORDER if r in counts)
             + (f"  (pytest exit status {run.run_end.get('exitstatus')})" if run.run_end else "  (the run did not finish)"),
             ""]
    rw = max([len(i["result"]) for _, i, _ in infos] + [6])
    nw = max([len(t.get("name") or "") for _, _, t in infos] + [4])
    lines.append(f"{'#':<4}{'result':<{rw + 2}}{'time':>8}  {'test':<{nw}}  why")
    for idx, info, t in infos:
        lines.append(f"{idx:<4}{info['result']:<{rw + 2}}{info['duration']:>6.1f} s  {(t.get('name') or ''):<{nw}}  "
                     f"{_why(info)}".rstrip())
    lines += ["", "Each test's log is <name>.log in this folder, next to its pcap (traffic tests).",
              f"Every SAI call of the run, in sairedis.rec grammar: ../{h.get('run')}.rec"]
    return "\n".join(lines) + "\n"


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

def run_name(jsonl_path):
    p = Path(jsonl_path)
    return p.name[: -len(".events.jsonl")] if p.name.endswith(".events.jsonl") else p.stem


def write_outputs(jsonl_path):
    """Write <run>/<stem>.log per test, <run>/<prefix>_index.log and <run>.rec next to
    <run>.events.jsonl. Returns (index path, rec path)."""
    events = load(jsonl_path)
    p = Path(jsonl_path)
    name = run_name(p)
    folder = p.with_name(name)
    folder.mkdir(parents=True, exist_ok=True)
    run = Run(events)
    width = max(2, len(str(run.collected or len(run.order))))
    infos = []
    for nodeid in run.order:
        t = run.tests[nodeid]
        ev = t["ev"]
        stem = ev.get("stem") or file_stem(run.header.get("run") or name, t["index"], ev.get("width") or width,
                                           ev.get("name") or nodeid.rsplit("::", 1)[-1])
        text, info = render_one(run, nodeid)
        (folder / f"{stem}.log").write_text(text, encoding="utf-8")
        infos.append((f"{t['index']:0{width}d}", info, ev))
    index_path = folder / index_name(run.header.get("run") or name)
    index_path.write_text(render_index(run, infos), encoding="utf-8")
    rec_path = p.with_name(name + ".rec")
    rec_path.write_text("\n".join(rec_lines(events)) + "\n", encoding="utf-8")
    return index_path, rec_path


def main(argv):
    if len(argv) != 2:
        print("usage: python3 -I testlog_render.py <run>.events.jsonl", file=sys.stderr)
        return 2
    index_path, rec_path = write_outputs(argv[1])
    print(f"test logs: {index_path.parent}/\nrec:       {rec_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
