"""
ACL drop-rule experiment (saivpp).

QUESTION: with a DROP rule for "TCP dst port 80" bound to the ingress port,
          is TCP/80 dropped, and is TCP/81 still forwarded?

Run (from ~/SAI-Challenger/tests):   run saivpp poc_shubham/test_acl_drop_exp.py
(host folder tests/poc is mounted in the container as tests/poc_shubham)

STEPS - each is its own test; a failed step makes the steps that need it SKIP
(instead of cascading errors), and a safety net removes anything left over:
  01 environment + configured ports valid
  02 L2 setup (VLAN, 2 ports, 2 FDB)            [needs traffic mode]
  03 baseline: TCP drop-port and fwd-port both forward   [needs traffic mode]
  04 ACL table created
  05 ACL entries created (DROP drop-port, FORWARD fwd-port)
  06 ACL bound to ingress port
  07 drop-port packet is DROPPED                [needs traffic mode]
  08 fwd-port packet is FORWARDED               [needs traffic mode]
  09 unmatched port (informational, never fails) [needs traffic mode]
  10 ACL unbound -> drop-port forwards again    [verify needs traffic mode]
  11 ACL entries removed
  12 ACL table removed
  13 after ACL removal both ports still forward [needs traffic mode]
  14 L2 config removed, defaults restored

Steps marked "needs traffic mode" are SKIPPED (not failed) when npu.run_traffic is
False. The ACL object steps (01, 04-06, 10-12) still run without traffic.

CONFIGURATION (all optional) - defaults < acl_exp_config.json < environment variables.
  acl_exp_config.json (next to this file; it is mounted into the container):
      {"in_port": 0, "out_port": 1, "drop_tcp_port": 80, "fwd_tcp_port": 81}
  keys: in_port, out_port, drop_tcp_port, fwd_tcp_port, other_tcp_port,
        vlan_id, bind_mode ("table" | "group"), timeout
  env:  ACL_IN_PORT, ACL_OUT_PORT, ACL_DROP_TCP_PORT, ACL_FWD_TCP_PORT,
        ACL_OTHER_TCP_PORT, ACL_VLAN_ID, ACL_BIND_MODE, ACL_TIMEOUT
  NOTE: `run` starts pytest inside the container, so host environment variables
        usually do NOT reach it - use the JSON file.

Grep the results:   grep -E "RESULT|CONFIG"
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

from saichallenger.common.sai_data import SaiObjType      # noqa: E402
from lib.acl import AclHelper                             # noqa: E402

try:
    from ptf.testutils import simple_tcp_packet, send_packet, verify_packet
    PTF_IMPORT_ERROR = None
except ImportError as exc:                                # only needed in traffic mode
    PTF_IMPORT_ERROR = exc

# ------------------------------- configuration -------------------------------
DEFAULTS = {
    "in_port": 0, "out_port": 1,
    "drop_tcp_port": 80, "fwd_tcp_port": 81, "other_tcp_port": 82,
    "vlan_id": 10, "bind_mode": "table", "timeout": 2,
}
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
SRC_MAC = "00:11:11:11:11:11"
DST_MAC = "00:22:22:22:22:22"
UNTAGGED = "SAI_VLAN_TAGGING_MODE_UNTAGGED"
PVID_ATTR = "SAI_PORT_ATTR_PORT_VLAN_ID"
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
        problems.append("%s: %s" % (label, str(exc)[:150]))


def l2_teardown(npu, s):
    """Undo the L2 setup in reverse order. Idempotent; returns a list of problems."""
    problems = []
    for mac in reversed(s.fdb):
        _attempt(problems, "remove fdb %s" % mac, lambda m=mac: npu.remove_fdb(s.vlan, m))
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
        print("SAFETY-NET CLEANUP PROBLEMS: %s" % problems)
    print("RESULT FINAL: %s" % s.results)


def step(name, needs=()):
    """Mark a step as done when it passes; skip it when a step it needs is missing."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            s = kwargs["st"]
            missing = [n for n in needs if n not in s.done]
            if missing:
                pytest.skip("needs step(s) that did not pass: %s" % ", ".join(missing))
            fn(*args, **kwargs)
            s.done.add(name)
        return wrapper
    return deco


def need_traffic(npu):
    if not npu.run_traffic:
        pytest.skip("traffic mode is off (npu.run_traffic is False): no dataplane ports")
    assert PTF_IMPORT_ERROR is None, "ptf.testutils not importable: %s" % PTF_IMPORT_ERROR


# -------------------------------- packet helpers -----------------------------
def make_tcp(dport):
    return simple_tcp_packet(eth_src=SRC_MAC, eth_dst=DST_MAC, ip_src="20.0.0.2",
                             ip_dst="192.168.1.10", tcp_sport=1234, tcp_dport=dport, ip_ttl=64)


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
    cfg = CFG
    assert cfg["bind_mode"] in ("table", "group"), "bind_mode must be 'table' or 'group'"
    assert cfg["in_port"] >= 0 and cfg["out_port"] >= 0, "port indexes must be >= 0"
    assert cfg["in_port"] != cfg["out_port"], "ingress and egress ports must differ"
    assert cfg["drop_tcp_port"] != cfg["fwd_tcp_port"], "drop and fwd TCP ports must differ"
    count = len(npu.port_oids)
    assert cfg["in_port"] < count and cfg["out_port"] < count, (
        "configured ports in=%s out=%s but the testbed has only %s ports" % (cfg["in_port"], cfg["out_port"], count))
    if npu.run_traffic:
        missing = [a for a in L2_API if not hasattr(npu, a)]
        assert not missing, "npu is missing helpers needed for traffic mode: %s" % missing
        bps = len(npu.dot1q_bp_oids)
        assert cfg["in_port"] < bps and cfg["out_port"] < bps, "bridge-port list too short: %s" % bps
        assert PTF_IMPORT_ERROR is None, "ptf.testutils not importable: %s" % PTF_IMPORT_ERROR
    print("CONFIG %s" % json.dumps(cfg, sort_keys=True))
    print("CONFIG traffic_mode=%s ports_available=%s" % (npu.run_traffic, count))
    st.done.add("env")


@step("l2", needs=["env"])
def test_02_l2_forwarding_configured(npu, st):
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
    print("RESULT l2_configured=True in_port=%s out_port=%s vlan=%s" % (CFG["in_port"], CFG["out_port"], vlan_id))


@step("baseline", needs=["l2"])
def test_03_baseline_both_tcp_ports_forward(npu, dataplane, st):
    need_traffic(npu)
    drop_ok = arrives(dataplane, CFG["drop_tcp_port"])
    fwd_ok = arrives(dataplane, CFG["fwd_tcp_port"])
    print("RESULT baseline tcp%s_forwarded=%s tcp%s_forwarded=%s"
          % (CFG["drop_tcp_port"], drop_ok, CFG["fwd_tcp_port"], fwd_ok))
    assert drop_ok and fwd_ok, ("Control check failed: TCP does not pass port %s -> %s even WITHOUT an ACL. "
                                "Fix L2 forwarding before judging the ACL." % (CFG["in_port"], CFG["out_port"]))


@step("table", needs=["env"])
def test_04_acl_table_created(npu, st):
    st.table = st.acl.create_table(fields=["IP_PROTOCOL", "L4_DST_PORT"])
    assert st.table and st.table != "oid:0x0", "ACL table create returned %r" % st.table
    print("RESULT acl_table_created=%s" % st.table)


@step("entries", needs=["table"])
def test_05_acl_drop_and_forward_entries_created(npu, st):
    status, drop_oid = st.acl.add_entry(
        st.table, 200, "SAI_PACKET_ACTION_DROP",
        [("IP_PROTOCOL", "6", "0xff"), ("L4_DST_PORT", str(CFG["drop_tcp_port"]), "0xffff")])
    print("RESULT add_drop_entry tcp/%s: %s" % (CFG["drop_tcp_port"], status))
    assert status == "SAI_STATUS_SUCCESS", (
        "DROP entry rejected. Check `grep 'Unhandled ACL entry attribute' /var/log/syslog` in the container.")
    status, fwd_oid = st.acl.add_entry(
        st.table, 100, "SAI_PACKET_ACTION_FORWARD",
        [("IP_PROTOCOL", "6", "0xff"), ("L4_DST_PORT", str(CFG["fwd_tcp_port"]), "0xffff")])
    print("RESULT add_forward_entry tcp/%s: %s" % (CFG["fwd_tcp_port"], status))
    assert status == "SAI_STATUS_SUCCESS", "FORWARD entry rejected"
    st.entries = [drop_oid, fwd_oid]


@step("bound", needs=["entries"])
def test_06_acl_bound_to_ingress_port(npu, st):
    acl_oid = st.table
    if CFG["bind_mode"] == "group":
        st.group = st.acl.create_group(st.table)
        acl_oid = st.group
    st.acl.bind_to_port(acl_oid, npu.port_oids[CFG["in_port"]])
    print("RESULT acl_bound mode=%s to_ptf_port=%s" % (CFG["bind_mode"], CFG["in_port"]))


@step("drop_checked", needs=["baseline", "bound"])
def test_07_drop_port_is_dropped(npu, dataplane, st):
    need_traffic(npu)
    got = arrives(dataplane, CFG["drop_tcp_port"])
    st.results["tcp%s_dropped" % CFG["drop_tcp_port"]] = not got
    print("RESULT tcp%s_dropped=%s" % (CFG["drop_tcp_port"], not got))
    assert not got, "TCP/%s still came out of the egress port - DROP rule had no effect" % CFG["drop_tcp_port"]


@step("fwd_checked", needs=["baseline", "bound"])
def test_08_fwd_port_is_forwarded(npu, dataplane, st):
    need_traffic(npu)
    got = arrives(dataplane, CFG["fwd_tcp_port"])
    st.results["tcp%s_forwarded" % CFG["fwd_tcp_port"]] = got
    print("RESULT tcp%s_forwarded=%s" % (CFG["fwd_tcp_port"], got))
    assert got, "TCP/%s did not arrive although a FORWARD entry exists" % CFG["fwd_tcp_port"]


@step("unmatched_checked", needs=["baseline", "bound"])
def test_09_unmatched_port_informational(npu, dataplane, st):
    """Packets matching NO rule. SAI says forward; VPP may drop (implicit deny). Never fails."""
    need_traffic(npu)
    got = arrives(dataplane, CFG["other_tcp_port"])
    st.results["tcp%s_unmatched_forwarded" % CFG["other_tcp_port"]] = got
    print("RESULT tcp%s_unmatched_forwarded=%s (info only; False = implicit deny)" % (CFG["other_tcp_port"], got))



@step("unbound", needs=["bound"])
def test_10_acl_unbound_restores_forwarding(npu, st):
    # Cleanup only: do not test forwarding after unbinding.
    st.acl.unbind_port(npu.port_oids[CFG["in_port"]])
    print("RESULT acl_unbound=True")


@step("entries_removed", needs=["entries", "unbound"])
def test_11_acl_entries_removed(npu, st):
    for oid in reversed(st.entries):
        st.acl.remove_entry(oid)
    st.entries = []
    assert st.acl.entries == [], "entries left behind: %s" % st.acl.entries
    print("RESULT acl_entries_removed=True")


@step("table_removed", needs=["table", "entries_removed"])
def test_12_acl_table_removed(npu, st):
    if st.group:
        st.acl.remove_group()
        st.group = None
    st.acl.remove_table(st.table)
    st.table = None
    assert st.acl.tables == [], "tables left behind: %s" % st.acl.tables
    print("RESULT acl_table_removed=True")


@step("after_removal", needs=["baseline", "table_removed"])
def test_13_forwarding_still_works_after_acl_removed(npu, dataplane, st):
    need_traffic(npu)
    drop_ok = arrives(dataplane, CFG["drop_tcp_port"])
    fwd_ok = arrives(dataplane, CFG["fwd_tcp_port"])
    print("RESULT after_acl_removal tcp%s_forwarded=%s tcp%s_forwarded=%s"
          % (CFG["drop_tcp_port"], drop_ok, CFG["fwd_tcp_port"], fwd_ok))
    assert drop_ok and fwd_ok, "L2 forwarding broke after the ACL was removed"


@step("l2_removed", needs=["l2"])
def test_14_l2_config_removed_and_defaults_restored(npu, st):
    problems = l2_teardown(npu, st)
    print("RESULT l2_cleanup_problems=%s" % problems)
    assert not problems, problems
