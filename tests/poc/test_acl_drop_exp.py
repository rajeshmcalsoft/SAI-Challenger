"""ACL drop-rule experiment (saivpp, needs --traffic for the packet steps).

Question: with a DROP rule for TCP/80 bound to the ingress port, is TCP/80 dropped
and TCP/81 still forwarded?

14 steps, each its own test (a failed step makes dependent steps SKIP):
L2 setup, baseline, create table, add entries, bind, check drop/forward, unbind,
remove entries and table, check forwarding again, restore defaults.

Packet steps SKIP when npu.run_traffic is False; the ACL object steps still run.
Settings: data/ACL.json ("acl_exp") < acl_exp_config.json < ACL_* environment variables.
"""
import functools
import json
import os
import sys
from types import SimpleNamespace

import pytest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
if TEST_DIR not in sys.path:
    sys.path.insert(0, TEST_DIR)

from saichallenger.common.sai_data import SaiObjType

from lib.acl import UNBOUND, AclHelper
from lib.params import load

try:
    from ptf.testutils import send_packet, simple_tcp_packet, verify_packet
    PTF_IMPORT_ERROR = None
except ImportError as exc:                                # only needed in traffic mode
    PTF_IMPORT_ERROR = exc

# ------------------------------- configuration -------------------------------
# Defaults live in data/ACL.json ("acl_exp" section; "traffic" for the packet itself).
_P = load()
_TRAF = _P["traffic"]
_EXP = {k: v for k, v in _P["acl_exp"].items() if not k.startswith("_")}
DEFAULTS = {k: _EXP[k] for k in ("in_port", "out_port", "drop_tcp_port", "fwd_tcp_port",
                                 "other_tcp_port", "vlan_id", "bind_mode", "timeout")}
DROP_PRIO, FWD_PRIO, FIELDS = _EXP["drop_priority"], _EXP["fwd_priority"], _EXP["fields"]
PROTO_MATCH = (_P["acl"]["tcp_protocol"], _P["acl"]["protocol_mask"], _P["acl"]["l4_port_mask"])
ENV_KEYS = {
    "ACL_IN_PORT": "in_port", "ACL_OUT_PORT": "out_port",
    "ACL_DROP_TCP_PORT": "drop_tcp_port", "ACL_FWD_TCP_PORT": "fwd_tcp_port",
    "ACL_OTHER_TCP_PORT": "other_tcp_port", "ACL_VLAN_ID": "vlan_id",
    "ACL_BIND_MODE": "bind_mode", "ACL_TIMEOUT": "timeout",
}


def load_config():
    cfg = dict(DEFAULTS)
    path = os.path.join(TEST_DIR, "acl_exp_config.json")
    if os.path.isfile(path):
        with open(path) as f:
            cfg.update(json.load(f))
    for env, key in ENV_KEYS.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    for key in DEFAULTS:
        if key != "bind_mode":
            cfg[key] = int(cfg[key])
    cfg["bind_mode"] = str(cfg["bind_mode"]).lower()
    return cfg


CFG = load_config()
SRC_MAC = _TRAF["src_mac"]
DST_MAC = _TRAF["dst_mac"]
UNTAGGED = _TRAF["vlan_tagging_mode"]
PVID_ATTR = _TRAF["pvid_attr"]
L2_API = ["dot1q_bp_oids", "default_vlan_oid", "default_vlan_id",
          "create_vlan_member", "remove_vlan_member", "create_fdb", "remove_fdb"]


# ------------------------- keep this module's state alive --------------------
@pytest.fixture(autouse=True)
def on_prev_test_failure():
    """Overrides the conftest.py fixture of the same name for THIS module only.
    The conftest version calls npu.reset() after any failed test, which would wipe
    the ACL objects these steps share. The `st` fixture below cleans up instead."""
    yield


def _attempt(problems, label, fn):
    try:
        fn()
    except Exception as exc:                              # noqa: BLE001
        problems.append(f"{label}: {str(exc)[:150]}")


def l2_teardown(npu, s):
    """Undo the L2 setup in reverse order. Idempotent; returns a list of problems."""
    problems = []
    for mac in reversed(s.fdb):
        _attempt(problems, f"remove fdb {mac}", lambda m=mac: npu.remove_fdb(s.vlan, m))
    s.fdb = []
    for oid in reversed(s.vlan_members):
        _attempt(problems, "remove vlan member", lambda o=oid: npu.remove(o))
    s.vlan_members = []
    for bp in s.default_removed:
        _attempt(problems, "restore default vlan member",
                 lambda b=bp: npu.create_vlan_member(npu.default_vlan_oid, b, UNTAGGED))
    s.default_removed = []
    for port in s.pvid_changed:
        _attempt(problems, "restore pvid", lambda p=port: npu.set(p, [PVID_ATTR, npu.default_vlan_id]))
    s.pvid_changed = []
    if s.vlan:
        _attempt(problems, "remove vlan", lambda: npu.remove(s.vlan))
        s.vlan = None
    return problems


@pytest.fixture(scope="module")
def st(npu):
    s = SimpleNamespace(acl=AclHelper(npu), done=set(), results={},
                        vlan=None, vlan_members=[], default_removed=[], fdb=[], pvid_changed=[],
                        table=None, group=None, entries=[])
    yield s
    problems = s.acl.cleanup() + l2_teardown(npu, s)      # safety net for leftovers
    if problems:
        print(f"SAFETY-NET CLEANUP PROBLEMS: {problems}")
    print(f"RESULT FINAL: {s.results}")


def step(name, needs=()):
    """Mark a step as done when it passes; skip it when a step it needs is missing."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            s = kwargs["st"]
            missing = [n for n in needs if n not in s.done]
            if missing:
                pytest.skip("needs step(s) that did not pass: {}".format(", ".join(missing)))
            fn(*args, **kwargs)
            s.done.add(name)
        return wrapper
    return deco


def need_traffic(npu):
    if not npu.run_traffic:
        pytest.skip("traffic mode is off (npu.run_traffic is False): no dataplane ports")
    assert PTF_IMPORT_ERROR is None, f"ptf.testutils not importable: {PTF_IMPORT_ERROR}"


# -------------------------------- packet helpers -----------------------------
def make_tcp(dport):
    return simple_tcp_packet(eth_src=SRC_MAC, eth_dst=DST_MAC, ip_src=_TRAF["ip_src"],
                             ip_dst=_TRAF["ip_dst"], tcp_sport=_TRAF["tcp_sport"], tcp_dport=dport,
                             ip_ttl=_TRAF["ip_ttl"])


def arrives(dataplane, dport):
    """Send TCP/dport into the ingress PTF port; True if it comes out of the egress port."""
    pkt = make_tcp(dport)
    send_packet(dataplane, CFG["in_port"], pkt)
    try:
        verify_packet(dataplane, pkt, CFG["out_port"], timeout=CFG["timeout"])
        return True
    except AssertionError:
        return False


# =================================== STEPS ===================================
def test_01_environment_and_ports_configured(npu, st):
    """Check the configuration (acl_exp_config.json) and that the test bed has what the experiment needs."""
    cfg = CFG
    assert cfg["bind_mode"] in ("table", "group"), "bind_mode must be 'table' or 'group'"
    assert cfg["in_port"] >= 0 and cfg["out_port"] >= 0, "port indexes must be >= 0"
    assert cfg["in_port"] != cfg["out_port"], "ingress and egress ports must differ"
    assert cfg["drop_tcp_port"] != cfg["fwd_tcp_port"], "drop and fwd TCP ports must differ"
    count = len(npu.port_oids)
    assert cfg["in_port"] < count and cfg["out_port"] < count, (
        "configured ports in={} out={} but the testbed has only {} ports".format(cfg["in_port"], cfg["out_port"], count))
    if npu.run_traffic:
        missing = [a for a in L2_API if not hasattr(npu, a)]
        assert not missing, f"npu is missing helpers needed for traffic mode: {missing}"
        bps = len(npu.dot1q_bp_oids)
        assert cfg["in_port"] < bps and cfg["out_port"] < bps, f"bridge-port list too short: {bps}"
        assert PTF_IMPORT_ERROR is None, f"ptf.testutils not importable: {PTF_IMPORT_ERROR}"
    print(f"CONFIG {json.dumps(cfg, sort_keys=True)}")
    print(f"CONFIG traffic_mode={npu.run_traffic} ports_available={count}")
    st.done.add("env")


# Set up VLAN and FDB between the two ports.
@step("l2", needs=["env"])
def test_02_l2_forwarding_configured(npu, st):
    """Build VLAN + FDB so a frame entering the ingress port is forwarded out of the egress port."""
    need_traffic(npu)
    ingress, egress = npu.port_oids[CFG["in_port"]], npu.port_oids[CFG["out_port"]]
    in_bp, out_bp = npu.dot1q_bp_oids[CFG["in_port"]], npu.dot1q_bp_oids[CFG["out_port"]]
    vlan_id = str(CFG["vlan_id"])
    st.vlan = npu.create(SaiObjType.VLAN, ["SAI_VLAN_ATTR_VLAN_ID", vlan_id])
    for port, bp in ((ingress, in_bp), (egress, out_bp)):
        npu.remove_vlan_member(npu.default_vlan_oid, bp)
        st.default_removed.append(bp)
        st.vlan_members.append(npu.create_vlan_member(st.vlan, bp, UNTAGGED))
        npu.set(port, [PVID_ATTR, vlan_id])
        st.pvid_changed.append(port)
    npu.create_fdb(st.vlan, SRC_MAC, in_bp)
    st.fdb.append(SRC_MAC)
    npu.create_fdb(st.vlan, DST_MAC, out_bp)
    st.fdb.append(DST_MAC)
    print("RESULT l2_configured=True in_port={} out_port={} vlan={}".format(CFG["in_port"], CFG["out_port"], vlan_id))


# Before any ACL, both TCP ports must get through.
@step("baseline", needs=["l2"])
def test_03_baseline_both_tcp_ports_forward(npu, dataplane, st):
    """Control: with NO ACL both TCP ports must be forwarded."""
    need_traffic(npu)
    drop_ok = arrives(dataplane, CFG["drop_tcp_port"])
    fwd_ok = arrives(dataplane, CFG["fwd_tcp_port"])
    print("RESULT baseline tcp{}_forwarded={} tcp{}_forwarded={}".format(CFG["drop_tcp_port"], drop_ok, CFG["fwd_tcp_port"], fwd_ok))
    assert drop_ok and fwd_ok, (
        "Control check failed: TCP does not pass port {} -> {} even WITHOUT an ACL. "
        "Fix L2 forwarding before judging the ACL.".format(CFG["in_port"], CFG["out_port"]))


# Create the ACL table.
@step("table", needs=["env"])
def test_04_acl_table_created(npu, st):
    """Create the ACL table that will hold the drop and forward rules."""
    st.table = st.acl.create_table(fields=FIELDS)
    assert bool(st.table) and st.table != UNBOUND, f"ACL table create returned {st.table!r}"
    print(f"RESULT acl_table_created={st.table}")


# Add the DROP and FORWARD entries.
@step("entries", needs=["table"])
def test_05_acl_drop_and_forward_entries_created(npu, st):
    """Add the rules: DROP TCP/<drop port> priority 200, FORWARD TCP/<fwd port> priority 100."""
    status, drop_oid = st.acl.add_entry(
        st.table, DROP_PRIO, "SAI_PACKET_ACTION_DROP",
        [("IP_PROTOCOL", PROTO_MATCH[0], PROTO_MATCH[1]), ("L4_DST_PORT", str(CFG["drop_tcp_port"]), PROTO_MATCH[2])])
    print("RESULT add_drop_entry tcp/{}: {}".format(CFG["drop_tcp_port"], status))
    assert status == "SAI_STATUS_SUCCESS", (
        "DROP entry rejected. Check `grep 'Unhandled ACL entry attribute' /var/log/syslog` in the container.")
    status, fwd_oid = st.acl.add_entry(
        st.table, FWD_PRIO, "SAI_PACKET_ACTION_FORWARD",
        [("IP_PROTOCOL", PROTO_MATCH[0], PROTO_MATCH[1]), ("L4_DST_PORT", str(CFG["fwd_tcp_port"]), PROTO_MATCH[2])])
    print("RESULT add_forward_entry tcp/{}: {}".format(CFG["fwd_tcp_port"], status))
    assert status == "SAI_STATUS_SUCCESS", "FORWARD entry rejected"
    st.entries = [drop_oid, fwd_oid]


# Bind the ACL to the ingress port.
@step("bound", needs=["entries"])
def test_06_acl_bound_to_ingress_port(npu, st):
    """Bind the ACL to the ingress port so it starts filtering."""
    acl_oid = st.table
    if CFG["bind_mode"] == "group":
        st.group = st.acl.create_group(st.table)
        acl_oid = st.group
    status = st.acl.bind_to_port(acl_oid, npu.port_oids[CFG["in_port"]])
    assert status == "SAI_STATUS_SUCCESS", f"bind status: expected 'SAI_STATUS_SUCCESS', got {status!r}"
    print("RESULT acl_bound mode={} to_ptf_port={}".format(CFG["bind_mode"], CFG["in_port"]))


# The drop port must NOT come out of the egress port.
@step("drop_checked", needs=["baseline", "bound"])
def test_07_drop_port_is_dropped(npu, dataplane, st):
    """The DROP port matches the DROP rule: its packet must NOT come out."""
    need_traffic(npu)
    got = arrives(dataplane, CFG["drop_tcp_port"])
    st.results["tcp{}_dropped".format(CFG["drop_tcp_port"])] = not got
    print("RESULT tcp{}_dropped={}".format(CFG["drop_tcp_port"], not got))
    assert not got, "TCP/{} still came out of the egress port - DROP rule had no effect".format(CFG["drop_tcp_port"])


# The forward port must come out of the egress port.
@step("fwd_checked", needs=["baseline", "bound"])
def test_08_fwd_port_is_forwarded(npu, dataplane, st):
    """The FWD port matches the FORWARD rule: its packet must come out."""
    need_traffic(npu)
    got = arrives(dataplane, CFG["fwd_tcp_port"])
    st.results["tcp{}_forwarded".format(CFG["fwd_tcp_port"])] = got
    print("RESULT tcp{}_forwarded={}".format(CFG["fwd_tcp_port"], got))
    assert got, "TCP/{} did not arrive although a FORWARD entry exists".format(CFG["fwd_tcp_port"])


# A port with no rule: result is printed, never judged.
@step("unmatched_checked", needs=["baseline", "bound"])
def test_09_unmatched_port_informational(npu, dataplane, st):
    """Packets matching NO rule. SAI says forward; VPP may drop (implicit deny). Never fails."""
    need_traffic(npu)
    got = arrives(dataplane, CFG["other_tcp_port"])
    st.results["tcp{}_unmatched_forwarded".format(CFG["other_tcp_port"])] = got
    print("RESULT tcp{}_unmatched_forwarded={} (info only; False = implicit deny)".format(CFG["other_tcp_port"], got))



# Unbind the ACL from the port.
@step("unbound", needs=["bound"])
def test_10_acl_unbound_restores_forwarding(npu, st):
    """Unbind the ACL from the ingress port (cleanup only; forwarding is re-checked in step 13)."""
    status = st.acl.unbind_port(npu.port_oids[CFG["in_port"]])
    assert status == "SAI_STATUS_SUCCESS", f"unbind status: expected 'SAI_STATUS_SUCCESS', got {status!r}"
    print("RESULT acl_unbound=True")


# Remove the ACL entries.
@step("entries_removed", needs=["entries", "unbound"])
def test_11_acl_entries_removed(npu, st):
    """Remove both ACL entries (they must go before their table)."""
    for oid in reversed(st.entries):
        status = st.acl.remove_entry(oid)
        assert status == "SAI_STATUS_SUCCESS", f"remove entry {oid}: expected 'SAI_STATUS_SUCCESS', got {status!r}"
    st.entries = []
    assert st.acl.entries == [], f"entries left behind: {st.acl.entries}"
    print("RESULT acl_entries_removed=True")


# Remove the ACL table.
@step("table_removed", needs=["table", "entries_removed"])
def test_12_acl_table_removed(npu, st):
    """Remove the table group (if used) and the table."""
    if st.group:
        st.acl.remove_group()
        st.group = None
    status = st.acl.remove_table(st.table)
    assert status == "SAI_STATUS_SUCCESS", f"remove table status: expected 'SAI_STATUS_SUCCESS', got {status!r}"
    st.table = None
    assert st.acl.tables == [], f"tables left behind: {st.acl.tables}"
    print("RESULT acl_table_removed=True")


# With the ACL gone, traffic must pass again.
@step("after_removal", needs=["baseline", "table_removed"])
def test_13_forwarding_still_works_after_acl_removed(npu, dataplane, st):
    """With the ACL gone both TCP ports must be forwarded again."""
    need_traffic(npu)
    drop_ok = arrives(dataplane, CFG["drop_tcp_port"])
    fwd_ok = arrives(dataplane, CFG["fwd_tcp_port"])
    print("RESULT after_acl_removal tcp{}_forwarded={} tcp{}_forwarded={}".format(CFG["drop_tcp_port"], drop_ok, CFG["fwd_tcp_port"], fwd_ok))
    assert drop_ok and fwd_ok, "L2 forwarding broke after the ACL was removed"


# Remove VLAN/FDB and restore default VLAN and PVID.
@step("l2_removed", needs=["l2"])
def test_14_l2_config_removed_and_defaults_restored(npu, st):
    """Undo the L2 setup: the switch is left as it was found."""
    problems = l2_teardown(npu, st)
    print(f"RESULT l2_cleanup_problems={problems}")
    assert problems == [], problems
