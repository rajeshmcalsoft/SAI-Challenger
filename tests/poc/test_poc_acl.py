"""Track C (ACL) PoC tests: Level 1 on both targets, plus the Level 2 drop and
permit-priority tests.

Built only on lib/acl.py (AclHelper) and the npu fixture. Ports come from the
poc_ports fixture or from the named port indexes below; no fixed port numbers
inside the tests.

No target checks (no "if saivpp"): SRC_IPV6 and DST_MAC are written to the SAI
spec (entry create must succeed). saivpp's rejection of them is declared as an
expected gap in tests/poc/targets/saivpp.json, not in this file.

Level 2 (class TestPocAclPermitPriority) checks that a higher-priority FORWARD
entry beats a lower-priority DROP entry for the same packet.

Level 2 (class TestPocAclDrop) is the 7 Oct drop-rule experiment moved here
with its step-by-step structure kept (see the class docstring for why). The ACL
object steps run on every target; the steps that send packets run only when
npu.run_traffic is True (saivpp with --traffic) and SKIP otherwise.

AclHelper API reminders:
  create_table(fields)  -> table oid (raises if SAI refuses)
  add_entry(...)        -> (status, oid), never raises
  create_group(table)   -> group oid (also creates one member)
  bind_to_port / unbind_port / remove_entry / remove_table -> None, raise on failure
The `acl` fixture removes whatever a test left behind, in reverse order, even
when the test fails.
"""
import functools
import os
import sys
from types import SimpleNamespace

import pytest

# lib/ sits next to this file; make it importable when the file is run alone.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from saichallenger.common.sai_data import SaiObjType

from lib.acl import OK, PORT_ACL_ATTR, UNBOUND, AclHelper

try:
    from ptf.testutils import send_packet, simple_tcp_packet, verify_packet
    PTF_IMPORT_ERROR = None
except ImportError as exc:                    # only needed when packets are sent
    PTF_IMPORT_ERROR = exc

pytestmark = [pytest.mark.acl]

BASIC_FIELDS = ["IP_PROTOCOL", "L4_DST_PORT"]
IPV6_FIELDS = ["SRC_IPV6"]
MAC_FIELDS = ["DST_MAC"]

# Level 2 drop test: traffic goes in on IN_IDX and must come out of OUT_IDX.
IN_IDX, OUT_IDX = 0, 1
DROP_PORT, FWD_PORT, OTHER_PORT = 80, 81, 82
L2_VLAN_ID = "10"
L2_SRC_MAC = "00:11:11:11:11:11"
L2_DST_MAC = "00:22:22:22:22:22"
UNTAGGED = "SAI_VLAN_TAGGING_MODE_UNTAGGED"
PVID_ATTR = "SAI_PORT_ATTR_PORT_VLAN_ID"

# Permit-priority test: three TCP destination ports, each tests one thing.
PRIO_PORT = 80       # DROP (low priority) and FORWARD (high priority): permit must win
CTRL_DROP_PORT = 81  # DROP only: proves the drop entries really work in this table
REVERSE_PORT = 82    # FORWARD (low) and DROP (high): drop should win (info only)
LOW_PRIO, HIGH_PRIO = 100, 200

# How the drop test binds its ACL to the ingress port: "group" binds an ACL table
# group that holds the table (what SONiC does); "table" binds the table directly.
BIND_MODE = "group"


# TEMPORARY: poc_ports is not in poc/conftest.py yet (Rajesh's task).
# Delete this fixture once his conftest provides it, or it will shadow his.
@pytest.fixture
def poc_ports(npu):
    return npu.port_oids[:4]


@pytest.fixture
def acl(npu):
    helper = AclHelper(npu)
    yield helper
    problems = helper.cleanup()
    assert not problems, f"cleanup left objects behind: {problems}"


def read_attr(npu, oid, attr, default=""):
    """Read one attribute and return it as a plain string."""
    data = npu.get(oid, [attr, default])
    value = data.value() if hasattr(data, "value") else data
    return str(value)


def _attempt(problems, label, fn):
    try:
        fn()
    except Exception as exc:                  # noqa: BLE001
        problems.append(f"{label}: {str(exc)[:150]}")


def l2_teardown(npu, s):
    """Undo the L2 setup in reverse order. Safe to call twice; returns problems."""
    problems = []
    for mac in reversed(s.fdb):
        _attempt(problems, f"remove fdb {mac}",
                 lambda m=mac: npu.remove_fdb(s.vlan, m))
    s.fdb = []
    for oid in reversed(s.vlan_members):
        _attempt(problems, "remove vlan member", lambda o=oid: npu.remove(o))
    s.vlan_members = []
    for bp in s.default_removed:
        _attempt(problems, "restore default vlan member",
                 lambda b=bp: npu.create_vlan_member(
                     npu.default_vlan_oid, b, UNTAGGED))
    s.default_removed = []
    for port in s.pvid_changed:
        _attempt(problems, "restore pvid",
                 lambda p=port: npu.set(p, [PVID_ATTR, npu.default_vlan_id]))
    s.pvid_changed = []
    if s.vlan:
        _attempt(problems, "remove vlan", lambda: npu.remove(s.vlan))
        s.vlan = None
    return problems


def l2_setup(npu, s):
    """Put IN_IDX and OUT_IDX into VLAN 10 as untagged members and add one static
    FDB entry per MAC. Everything is recorded in `s` so l2_teardown can undo it,
    even when this stops halfway."""
    s.vlan = npu.create(SaiObjType.VLAN, ["SAI_VLAN_ATTR_VLAN_ID", L2_VLAN_ID])
    for idx in (IN_IDX, OUT_IDX):
        bp, port = npu.dot1q_bp_oids[idx], npu.port_oids[idx]
        npu.remove_vlan_member(npu.default_vlan_oid, bp)
        s.default_removed.append(bp)
        s.vlan_members.append(npu.create_vlan_member(s.vlan, bp, UNTAGGED))
        npu.set(port, [PVID_ATTR, L2_VLAN_ID])
        s.pvid_changed.append(port)
    for mac, idx in ((L2_SRC_MAC, IN_IDX), (L2_DST_MAC, OUT_IDX)):
        npu.create_fdb(s.vlan, mac, npu.dot1q_bp_oids[idx])
        s.fdb.append(mac)


def arrives(dataplane, dport):
    """Send TCP/dport into IN_IDX; True if it comes out of OUT_IDX."""
    pkt = simple_tcp_packet(eth_src=L2_SRC_MAC, eth_dst=L2_DST_MAC,
                            ip_src="20.0.0.2", ip_dst="192.168.1.10",
                            tcp_sport=1234, tcp_dport=dport, ip_ttl=64)
    send_packet(dataplane, IN_IDX, pkt)
    try:
        verify_packet(dataplane, pkt, OUT_IDX, timeout=2)
        return True
    except AssertionError:
        return False


def tcp_match(dport):
    return [("IP_PROTOCOL", "6", "0xff"), ("L4_DST_PORT", str(dport), "0xffff")]


# ------------------------------- Level 1 tests -------------------------------
def test_poc_acl_table_create_remove(acl):
    table = acl.create_table(BASIC_FIELDS)
    assert table != UNBOUND
    acl.remove_table(table)


def test_poc_acl_table_readback(acl, npu):
    table = acl.create_table(BASIC_FIELDS, stage="INGRESS", bind_point="PORT")
    stage = read_attr(npu, table, "SAI_ACL_TABLE_ATTR_ACL_STAGE")
    assert "INGRESS" in stage, f"stage readback: {stage}"


def test_poc_acl_entry_drop_create_remove(acl):
    table = acl.create_table(BASIC_FIELDS)
    status, entry = acl.add_entry(table, 10, "SAI_PACKET_ACTION_DROP",
                                  [("L4_DST_PORT", "80", "0xffff")])
    assert status == OK, f"entry create failed: {status}"
    acl.remove_entry(entry)


def test_poc_acl_entry_priority_readback(acl, npu):
    table = acl.create_table(BASIC_FIELDS)
    status, entry = acl.add_entry(table, 42, "SAI_PACKET_ACTION_DROP",
                                  [("L4_DST_PORT", "80", "0xffff")])
    assert status == OK, f"entry create failed: {status}"
    prio = read_attr(npu, entry, "SAI_ACL_ENTRY_ATTR_PRIORITY")
    assert prio == "42", f"priority readback: {prio}"


def test_poc_acl_bind_port(acl, npu, poc_ports):
    port = poc_ports[0]
    table = acl.create_table(BASIC_FIELDS)
    acl.bind_to_port(table, port)
    bound = read_attr(npu, port, PORT_ACL_ATTR, UNBOUND)
    assert bound == table, f"port ACL readback: {bound}"
    acl.unbind_port(port)
    assert read_attr(npu, port, PORT_ACL_ATTR, UNBOUND) == UNBOUND


def test_poc_acl_match_src_ipv6(acl):
    table = acl.create_table(IPV6_FIELDS)
    status, entry = acl.add_entry(
        table, 10, "SAI_PACKET_ACTION_DROP",
        [("SRC_IPV6", "2001:db8::1", "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff")])
    assert status == OK, f"SRC_IPV6 entry create failed: {status}"
    acl.remove_entry(entry)


def test_poc_acl_match_dst_mac(acl):
    table = acl.create_table(MAC_FIELDS)
    status, entry = acl.add_entry(
        table, 10, "SAI_PACKET_ACTION_DROP",
        [("DST_MAC", "00:11:22:33:44:55", "FF:FF:FF:FF:FF:FF")])
    assert status == OK, f"DST_MAC entry create failed: {status}"
    acl.remove_entry(entry)


def test_poc_acl_table_group_create_remove(npu, acl):
    """Verify ACL table-group and member creation and removal on the target."""

    table_oid = None
    group_oid = None
    cleanup_problems = []

    try:
        # 1. Create an ACL table
        table_oid = acl.create_table(
            fields=["IP_PROTOCOL", "L4_DST_PORT"],
            stage="INGRESS",
            bind_point="PORT",
        )
        assert table_oid, "ACL table creation returned no OID"
        print(f"RESULT acl_table_created={table_oid}")

        # 2. Create the ACL table group and its member
        group_oid = acl.create_group(
            table=table_oid,
            stage="INGRESS",
            bind_point="PORT",
            priority="100",
        )
        assert group_oid, "ACL table-group creation returned no OID"
        print(f"RESULT acl_table_group_created={group_oid}")

        assert acl.groups, "ACL helper did not record the created group"
        assert acl.members, "ACL helper did not record the created group member"
        print(f"RESULT acl_table_group_member_created={acl.members[-1]}")

    finally:
        # The helper removes dependent objects before their parents.
        cleanup_problems = acl.cleanup()
        print(f"RESULT acl_group_cleanup_problems={cleanup_problems}")

    assert not cleanup_problems, (
        f"ACL table-group cleanup failed: {cleanup_problems}"
    )
    print("RESULT acl_table_group_create_remove=PASS")


@pytest.mark.negative
def test_poc_acl_entry_unknown_field_rejected(acl):
    """An entry that matches on a field its table does not have must be refused.
    The table has only IP_PROTOCOL and L4_DST_PORT; the entry asks for
    L4_SRC_PORT. If the target accepts it, the test FAILS. Declaring that as an
    expected gap (xfail + reason) is done in tests/poc/targets/<target>.json, not
    here."""
    table = acl.create_table(BASIC_FIELDS)
    try:
        status, _ = acl.add_entry(table, 10, "SAI_PACKET_ACTION_DROP",
                                  [("L4_SRC_PORT", "1234", "0xffff")])
    except Exception as exc:                  # noqa: BLE001
        print(f"RESULT unknown_field refused with exception: {str(exc)[:120]}")
        return
    assert status != OK, "target accepted an ACL entry with a field its table lacks"
    print(f"RESULT unknown_field refused with status {status}")


# ------------------------------- Level 2 tests -------------------------------
def step(name, needs=()):
    """Mark a step as done when it passes; SKIP it when a step it needs is missing."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            state = kwargs["st"]
            missing = [n for n in needs if n not in state.done]
            if missing:
                pytest.skip("needs step(s) that did not pass: " + ", ".join(missing))
            fn(*args, **kwargs)
            state.done.add(name)
        return wrapper
    return deco


def need_traffic(npu):
    if not npu.run_traffic:
        pytest.skip("traffic mode is off (npu.run_traffic is False): no dataplane ports")
    assert PTF_IMPORT_ERROR is None, f"ptf.testutils not importable: {PTF_IMPORT_ERROR}"


@pytest.fixture(scope="class")
def st(npu):
    """State shared by the steps of ONE test class (TestPocAclDrop,
    TestPocAclPermitPriority). Each class gets a fresh state and, at the end of the
    class, anything a failed step left behind is removed, so the switch is left as
    it was found and a failure in one class cannot break the next."""
    state = SimpleNamespace(acl=AclHelper(npu), done=set(), table=None, group=None,
                            entries=[], vlan=None, vlan_members=[],
                            default_removed=[], fdb=[], pvid_changed=[])
    yield state
    problems = state.acl.cleanup() + l2_teardown(npu, state)
    assert not problems, f"cleanup left objects behind: {problems}"


class TestPocAclDrop:
    """ACL drop rule: ingress ACL on port IN_IDX with DROP TCP/80 (priority 200)
    and FORWARD TCP/81 (priority 100); TCP/80 must not come out of OUT_IDX,
    TCP/81 must. TCP/82 matches no entry: its result is printed and never fails.

    The steps are separate tests that share state, exactly like the experiment
    in poc_shubham/test_acl_drop_exp.py. That structure is kept on purpose: the
    same checks written as ONE test function passed every control-plane call but
    TCP/80 was still forwarded on saivpp after 5 s of probing (18 Oct, two runs),
    while the step version passes. The cause of the difference is not known.

    BIND_MODE (top of the file) chooses whether the ACL table or an ACL table group
    holding it is bound to the port; the experiment can use either through
    acl_exp_config.json, and the RESULT acl_bound line prints which one ran.
    """

    @pytest.fixture(autouse=True)
    def on_prev_test_failure(self):
        """Overrides the conftest fixture of the same name for THIS class only.
        The conftest version resets the npu after a failed test, which would wipe
        the ACL objects the steps share. The `st` fixture cleans up instead."""
        yield

    @pytest.mark.traffic
    @step("l2")
    def test_poc_acl_drop_01_l2_configured(self, npu, st):
        need_traffic(npu)
        l2_setup(npu, st)

    @pytest.mark.traffic
    @step("baseline", needs=["l2"])
    def test_poc_acl_drop_02_baseline_both_ports_forward(self, npu, dataplane, st):
        need_traffic(npu)
        drop_ok = arrives(dataplane, DROP_PORT)
        fwd_ok = arrives(dataplane, FWD_PORT)
        print(f"RESULT baseline tcp{DROP_PORT}_forwarded={drop_ok} "
              f"tcp{FWD_PORT}_forwarded={fwd_ok}")
        assert drop_ok and fwd_ok, (
            "control check failed: TCP does not pass the ports even without an ACL")

    @step("table")
    def test_poc_acl_drop_03_table_created(self, npu, st):
        st.table = st.acl.create_table(BASIC_FIELDS)
        assert st.table and st.table != UNBOUND, f"ACL table create returned {st.table!r}"

    @step("entries", needs=["table"])
    def test_poc_acl_drop_04_entries_created(self, npu, st):
        status, drop_oid = st.acl.add_entry(
            st.table, 200, "SAI_PACKET_ACTION_DROP", tcp_match(DROP_PORT))
        assert status == OK, f"DROP entry create failed: {status}"
        status, fwd_oid = st.acl.add_entry(
            st.table, 100, "SAI_PACKET_ACTION_FORWARD", tcp_match(FWD_PORT))
        assert status == OK, f"FORWARD entry create failed: {status}"
        st.entries = [drop_oid, fwd_oid]

    @step("bound", needs=["entries"])
    def test_poc_acl_drop_05_bound_to_ingress_port(self, npu, st):
        acl_oid = st.table
        if BIND_MODE == "group":
            st.group = st.acl.create_group(st.table)
            acl_oid = st.group
        st.acl.bind_to_port(acl_oid, npu.port_oids[IN_IDX])
        print(f"RESULT acl_bound mode={BIND_MODE} to_port_index={IN_IDX}")

    @pytest.mark.traffic
    @step("drop_checked", needs=["baseline", "bound"])
    def test_poc_acl_drop_06_drop_port_is_dropped(self, npu, dataplane, st):
        need_traffic(npu)
        got = arrives(dataplane, DROP_PORT)
        print(f"RESULT tcp{DROP_PORT}_dropped={not got}")
        assert not got, (
            f"TCP/{DROP_PORT} still came out of the egress port: DROP had no effect")

    @pytest.mark.traffic
    @step("fwd_checked", needs=["baseline", "bound"])
    def test_poc_acl_drop_07_fwd_port_is_forwarded(self, npu, dataplane, st):
        need_traffic(npu)
        got = arrives(dataplane, FWD_PORT)
        print(f"RESULT tcp{FWD_PORT}_forwarded={got}")
        assert got, f"TCP/{FWD_PORT} did not arrive although a FORWARD entry exists"

    @pytest.mark.traffic
    @step("unmatched_checked", needs=["baseline", "bound"])
    def test_poc_acl_drop_08_unmatched_port_informational(self, npu, dataplane, st):
        need_traffic(npu)
        got = arrives(dataplane, OTHER_PORT)
        print(f"RESULT tcp{OTHER_PORT}_unmatched_forwarded={got} "
              "(info only; False = implicit deny)")

    @step("readback", needs=["bound"])
    def test_poc_acl_drop_09_port_acl_readback(self, npu, st):
        bound = read_attr(npu, npu.port_oids[IN_IDX], PORT_ACL_ATTR, UNBOUND)
        expected = st.group or st.table
        assert bound == expected, f"port ACL readback: {bound}, expected {expected}"

    @step("unbound", needs=["bound"])
    def test_poc_acl_drop_10_unbound(self, npu, st):
        # Cleanup only: forwarding after the unbind is checked in step 13.
        st.acl.unbind_port(npu.port_oids[IN_IDX])

    @step("entries_removed", needs=["entries", "unbound"])
    def test_poc_acl_drop_11_entries_removed(self, npu, st):
        for oid in reversed(st.entries):
            st.acl.remove_entry(oid)
        st.entries = []
        assert st.acl.entries == [], f"entries left behind: {st.acl.entries}"

    @step("table_removed", needs=["table", "entries_removed"])
    def test_poc_acl_drop_12_table_removed(self, npu, st):
        if st.group:
            st.acl.remove_group()
            st.group = None
        st.acl.remove_table(st.table)
        st.table = None
        assert st.acl.tables == [], f"tables left behind: {st.acl.tables}"

    @pytest.mark.traffic
    @step("after_removal", needs=["baseline", "table_removed"])
    def test_poc_acl_drop_13_forwarding_after_acl_removed(self, npu, dataplane, st):
        need_traffic(npu)
        drop_ok = arrives(dataplane, DROP_PORT)
        fwd_ok = arrives(dataplane, FWD_PORT)
        print(f"RESULT after_acl_removal tcp{DROP_PORT}_forwarded={drop_ok} "
              f"tcp{FWD_PORT}_forwarded={fwd_ok}")
        assert drop_ok and fwd_ok, "L2 forwarding broke after the ACL was removed"

    @step("l2_removed", needs=["l2"])
    def test_poc_acl_drop_14_l2_removed_and_defaults_restored(self, npu, st):
        problems = l2_teardown(npu, st)
        assert not problems, problems


class TestPocAclPermitPriority:
    """A higher-priority permit (FORWARD) overrides a lower-priority DROP.

    One ingress ACL table on port IN_IDX holds five entries (a bigger number
    means a higher priority in SAI):
      TCP/80  DROP    priority LOW_PRIO     TCP/80  FORWARD priority HIGH_PRIO
      TCP/81  DROP    priority LOW_PRIO     (no permit: control, drop must work)
      TCP/82  FORWARD priority LOW_PRIO     TCP/82  DROP    priority HIGH_PRIO

    TCP/80 must come out of OUT_IDX (permit wins). TCP/81 must NOT (the drop
    entries are live, so the TCP/80 result is not a pass by accident). TCP/82 is
    the mirror case; its result is printed and never fails the test.

    Same step-by-step structure and bind mode as TestPocAclDrop, for the same
    reason: that structure is the one proven on saivpp.
    """

    @pytest.fixture(autouse=True)
    def on_prev_test_failure(self):
        """Same override as in TestPocAclDrop: the `st` fixture cleans up."""
        yield

    @pytest.mark.traffic
    @step("l2")
    def test_poc_acl_permit_priority_01_l2_configured(self, npu, st):
        need_traffic(npu)
        l2_setup(npu, st)

    @pytest.mark.traffic
    @step("baseline", needs=["l2"])
    def test_poc_acl_permit_priority_02_baseline_all_ports_forward(
            self, npu, dataplane, st):
        need_traffic(npu)
        result = {p: arrives(dataplane, p)
                  for p in (PRIO_PORT, CTRL_DROP_PORT, REVERSE_PORT)}
        print(f"RESULT baseline forwarded={result}")
        assert all(result.values()), (
            f"control check failed: TCP does not pass without an ACL: {result}")

    @step("table")
    def test_poc_acl_permit_priority_03_table_created(self, npu, st):
        st.table = st.acl.create_table(BASIC_FIELDS)
        assert st.table and st.table != UNBOUND, f"ACL table create returned {st.table!r}"

    @step("entries", needs=["table"])
    def test_poc_acl_permit_priority_04_entries_created(self, npu, st):
        wanted = [
            (LOW_PRIO, "SAI_PACKET_ACTION_DROP", PRIO_PORT),
            (HIGH_PRIO, "SAI_PACKET_ACTION_FORWARD", PRIO_PORT),
            (LOW_PRIO, "SAI_PACKET_ACTION_DROP", CTRL_DROP_PORT),
            (LOW_PRIO, "SAI_PACKET_ACTION_FORWARD", REVERSE_PORT),
            (HIGH_PRIO, "SAI_PACKET_ACTION_DROP", REVERSE_PORT),
        ]
        for prio, action, port in wanted:
            status, oid = st.acl.add_entry(st.table, prio, action, tcp_match(port))
            assert status == OK, f"{action} TCP/{port} prio {prio} create failed: {status}"
            st.entries.append(oid)

    @step("bound", needs=["entries"])
    def test_poc_acl_permit_priority_05_bound_to_ingress_port(self, npu, st):
        acl_oid = st.table
        if BIND_MODE == "group":
            st.group = st.acl.create_group(st.table)
            acl_oid = st.group
        st.acl.bind_to_port(acl_oid, npu.port_oids[IN_IDX])
        print(f"RESULT acl_bound mode={BIND_MODE} to_port_index={IN_IDX}")

    @pytest.mark.traffic
    @step("permit_checked", needs=["baseline", "bound"])
    def test_poc_acl_permit_priority_06_permit_overrides_drop(self, npu, dataplane, st):
        need_traffic(npu)
        got = arrives(dataplane, PRIO_PORT)
        print(f"RESULT tcp{PRIO_PORT}_permit_wins={got}")
        assert got, (f"TCP/{PRIO_PORT} was dropped although a higher-priority "
                     "FORWARD entry exists")

    @pytest.mark.traffic
    @step("control_checked", needs=["baseline", "bound"])
    def test_poc_acl_permit_priority_07_drop_without_permit_is_dropped(
            self, npu, dataplane, st):
        need_traffic(npu)
        got = arrives(dataplane, CTRL_DROP_PORT)
        print(f"RESULT tcp{CTRL_DROP_PORT}_dropped={not got}")
        assert not got, (f"TCP/{CTRL_DROP_PORT} still came out: the DROP entries do not "
                         "work in this table, so step 06 proves nothing")

    @pytest.mark.traffic
    @step("reverse_checked", needs=["baseline", "bound"])
    def test_poc_acl_permit_priority_08_reverse_case_informational(
            self, npu, dataplane, st):
        need_traffic(npu)
        got = arrives(dataplane, REVERSE_PORT)
        print(f"RESULT tcp{REVERSE_PORT}_dropped={not got} "
              "(info only; True = higher-priority DROP beat the lower FORWARD)")

    @step("readback", needs=["entries"])
    def test_poc_acl_permit_priority_09_priorities_read_back(self, npu, st):
        low = read_attr(npu, st.entries[0], "SAI_ACL_ENTRY_ATTR_PRIORITY")
        high = read_attr(npu, st.entries[1], "SAI_ACL_ENTRY_ATTR_PRIORITY")
        assert (low, high) == (str(LOW_PRIO), str(HIGH_PRIO)), (
            f"priority readback: drop={low}, permit={high}")

    @step("unbound", needs=["bound"])
    def test_poc_acl_permit_priority_10_unbound(self, npu, st):
        st.acl.unbind_port(npu.port_oids[IN_IDX])

    @step("entries_removed", needs=["entries", "unbound"])
    def test_poc_acl_permit_priority_11_entries_removed(self, npu, st):
        for oid in reversed(st.entries):
            st.acl.remove_entry(oid)
        st.entries = []
        assert st.acl.entries == [], f"entries left behind: {st.acl.entries}"

    @step("table_removed", needs=["table", "entries_removed"])
    def test_poc_acl_permit_priority_12_table_removed(self, npu, st):
        if st.group:
            st.acl.remove_group()
            st.group = None
        st.acl.remove_table(st.table)
        st.table = None
        assert st.acl.tables == [], f"tables left behind: {st.acl.tables}"

    @pytest.mark.traffic
    @step("after_removal", needs=["baseline", "table_removed"])
    def test_poc_acl_permit_priority_13_forwarding_after_acl_removed(
            self, npu, dataplane, st):
        need_traffic(npu)
        result = {p: arrives(dataplane, p)
                  for p in (PRIO_PORT, CTRL_DROP_PORT, REVERSE_PORT)}
        print(f"RESULT after_acl_removal forwarded={result}")
        assert all(result.values()), f"L2 forwarding broke after the ACL was removed: {result}"

    @step("l2_removed", needs=["l2"])
    def test_poc_acl_permit_priority_14_l2_removed_and_defaults_restored(self, npu, st):
        problems = l2_teardown(npu, st)
        assert not problems, problems
