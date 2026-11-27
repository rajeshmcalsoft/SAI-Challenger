"""Track C (ACL) PoC tests.

Level 1 (control plane, all targets): create/remove tables and entries, readback, port bind, groups.
Level 2 (needs --traffic, saivpp): TestPocAclDrop (DROP/FORWARD rules) and
TestPocAclPermitPriority (higher priority number wins).

Built on lib/acl.py (AclHelper) and the npu fixture; values come from data/ACL.json.
No target checks here: known target bugs are listed in targets/<profile>.json.
The `acl` fixture removes whatever a test left behind, even when the test fails.
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
from lib.params import load

try:
    from ptf.testutils import send_packet, simple_tcp_packet, verify_packet
    PTF_IMPORT_ERROR = None
except ImportError as exc:                    # only needed when packets are sent
    PTF_IMPORT_ERROR = exc

pytestmark = [pytest.mark.acl]

# Every tunable value comes from data/ACL.json (see lib/params.py); nothing is hardcoded here.
_P = load()
_ACLP, _TRAF = _P["acl"], _P["traffic"]
_L1, _DROP, _PRIO = _ACLP["level1"], _ACLP["drop_test"], _ACLP["permit_priority_test"]

BASIC_FIELDS = _ACLP["basic_fields"]
IPV6_FIELDS = _ACLP["ipv6_fields"]
MAC_FIELDS = _ACLP["mac_fields"]

# Level 2 drop test: traffic goes in on IN_IDX and must come out of OUT_IDX.
IN_IDX, OUT_IDX = _TRAF["in_idx"], _TRAF["out_idx"]
DROP_PORT, FWD_PORT, OTHER_PORT = _DROP["drop_port"], _DROP["fwd_port"], _DROP["other_port"]
DROP_PRIO, FWD_PRIO = _DROP["drop_priority"], _DROP["fwd_priority"]
L2_VLAN_ID = _TRAF["vlan_id"]
L2_SRC_MAC = _TRAF["src_mac"]
L2_DST_MAC = _TRAF["dst_mac"]
UNTAGGED = _TRAF["vlan_tagging_mode"]
PVID_ATTR = _TRAF["pvid_attr"]

# Permit-priority test: three TCP destination ports, each tests one thing.
PRIO_PORT = _PRIO["prio_port"]            # DROP (low priority) and FORWARD (high priority): permit must win
CTRL_DROP_PORT = _PRIO["ctrl_drop_port"]  # DROP only: proves the drop entries really work in this table
REVERSE_PORT = _PRIO["reverse_port"]      # FORWARD (low) and DROP (high): drop should win (info only)
LOW_PRIO, HIGH_PRIO = _PRIO["low_prio"], _PRIO["high_prio"]

# How the drop test binds its ACL to the ingress port: "group" binds an ACL table
# group that holds the table (what SONiC does); "table" binds the table directly.
BIND_MODE = _ACLP["bind_mode"]


# TEMPORARY: poc_ports is not in poc/conftest.py yet (Rajesh's task).
# Delete this fixture once his conftest provides it, or it will shadow his.
@pytest.fixture
def poc_ports(npu):
    return npu.port_oids[:_ACLP["poc_ports_count"]]


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
                            ip_src=_TRAF["ip_src"], ip_dst=_TRAF["ip_dst"],
                            tcp_sport=_TRAF["tcp_sport"], tcp_dport=dport, ip_ttl=_TRAF["ip_ttl"])
    wait = _TRAF["verify_timeout_s"]
    send_packet(dataplane, IN_IDX, pkt)
    try:
        verify_packet(dataplane, pkt, OUT_IDX, timeout=wait)
        return True
    except AssertionError:
        return False


def tcp_match(dport):
    return [("IP_PROTOCOL", _ACLP["tcp_protocol"], _ACLP["protocol_mask"]),
            ("L4_DST_PORT", str(dport), _ACLP["l4_port_mask"])]


# ------------------------------- Level 1 tests -------------------------------
SUCCESS = OK
L1_PORT, L1_PORT_MASK = _L1["entry_l4_dst_port"], _ACLP["l4_port_mask"]
L1_PRIO, L1_PRIO_READBACK = _L1["drop_entry_priority"], _L1["priority_readback_value"]


# Create an ACL table, then remove it; both must succeed.
def test_poc_acl_table_create_remove(acl):
    """Create an ACL table, then remove it: the basic object life cycle."""
    table = acl.create_table(BASIC_FIELDS)
    assert table != UNBOUND, (
        f"create returned a real object id: expected 'an oid other than oid:0x0', got {table!r}")
    status = acl.remove_table(table)
    assert status == SUCCESS, f"remove status: expected {SUCCESS!r}, got {status!r}"


# Create a table and read its stage back; it must be INGRESS.
def test_poc_acl_table_readback(acl, npu):
    """Create a table and read its stage attribute back from SAI."""
    table = acl.create_table(BASIC_FIELDS, stage="INGRESS", bind_point="PORT")
    stage = read_attr(npu, table, "SAI_ACL_TABLE_ATTR_ACL_STAGE")
    assert "INGRESS" in stage, f"stage readback: {stage}"


# Add a DROP entry (TCP port) to a table, then remove it.
def test_poc_acl_entry_drop_create_remove(acl):
    """Add a DROP entry (one TCP destination port) to a table, then remove the entry."""
    table = acl.create_table(BASIC_FIELDS)
    status, entry = acl.add_entry(table, L1_PRIO, "SAI_PACKET_ACTION_DROP",
                                  [("L4_DST_PORT", L1_PORT, L1_PORT_MASK)])
    assert status == SUCCESS, f"entry create failed: {status}"
    status = acl.remove_entry(entry)
    assert status == SUCCESS, f"entry remove status: expected {SUCCESS!r}, got {status!r}"


# Set an entry priority and read it back; the values must match.
def test_poc_acl_entry_priority_readback(acl, npu):
    """Create an entry with a chosen priority and read the priority back from SAI."""
    table = acl.create_table(BASIC_FIELDS)
    status, entry = acl.add_entry(table, L1_PRIO_READBACK, "SAI_PACKET_ACTION_DROP",
                                  [("L4_DST_PORT", L1_PORT, L1_PORT_MASK)])
    assert status == SUCCESS, f"entry create failed: {status}"
    prio = read_attr(npu, entry, "SAI_ACL_ENTRY_ATTR_PRIORITY")
    assert prio == str(L1_PRIO_READBACK), f"priority readback: {prio}"


# Bind a table to a port and read it back, then unbind and read again.
def test_poc_acl_bind_port(acl, npu, poc_ports):
    """Bind an ACL table to a port, read the binding back, unbind, read again."""
    port = poc_ports[0]
    table = acl.create_table(BASIC_FIELDS)
    acl.bind_to_port(table, port)
    bound = read_attr(npu, port, PORT_ACL_ATTR, UNBOUND)
    assert bound == table, f"port ACL readback: {bound}"
    acl.unbind_port(port)
    after = read_attr(npu, port, PORT_ACL_ATTR, UNBOUND)
    assert after == UNBOUND, f"port's ingress ACL after unbind: expected {UNBOUND!r}, got {after!r}"


# An entry matching SRC_IPV6 must be accepted when the table enables it.
def test_poc_acl_match_src_ipv6(acl):
    """An entry matching SRC_IPV6 must be accepted when the table has SRC_IPV6 enabled."""
    table = acl.create_table(IPV6_FIELDS)
    v6 = _L1["src_ipv6"]
    status, entry = acl.add_entry(
        table, L1_PRIO, "SAI_PACKET_ACTION_DROP",
        [("SRC_IPV6", v6["value"], v6["mask"])])
    assert status == SUCCESS, f"SRC_IPV6 entry create failed: {status}"
    acl.remove_entry(entry)


# An entry matching DST_MAC must be accepted when the table enables it.
def test_poc_acl_match_dst_mac(acl):
    """An entry matching DST_MAC must be accepted when the table has DST_MAC enabled."""
    table = acl.create_table(MAC_FIELDS)
    mac = _L1["dst_mac"]
    status, entry = acl.add_entry(
        table, L1_PRIO, "SAI_PACKET_ACTION_DROP",
        [("DST_MAC", mac["value"], mac["mask"])])
    assert status == SUCCESS, f"DST_MAC entry create failed: {status}"
    acl.remove_entry(entry)


# Create a table group and member, then remove member, group, table.
def test_poc_acl_table_group_create_remove(npu, acl):
    """Verify ACL table-group and member creation and removal on the target."""

    table_oid = None
    group_oid = None
    cleanup_problems = []

    try:
        table_oid = acl.create_table(
            fields=BASIC_FIELDS,
            stage=_ACLP["stage"],
            bind_point=_ACLP["bind_point"],
        )
        assert bool(table_oid), "ACL table creation returned no OID"
        print(f"RESULT acl_table_created={table_oid}")

        group_oid = acl.create_group(
            table=table_oid,
            stage=_ACLP["stage"],
            bind_point=_ACLP["bind_point"],
            priority=_ACLP["group_member_priority"],
        )
        assert bool(group_oid), "ACL table-group creation returned no OID"
        print(f"RESULT acl_table_group_created={group_oid}")

        assert bool(acl.groups), "ACL helper did not record the created group"
        assert bool(acl.members), "ACL helper did not record the created group member"
        print(f"RESULT acl_table_group_member_created={acl.members[-1]}")

    finally:
        # The helper removes dependent objects before their parents.
        cleanup_problems = acl.cleanup()
        print(f"RESULT acl_group_cleanup_problems={cleanup_problems}")

    assert cleanup_problems == [], f"ACL table-group cleanup failed: {cleanup_problems}"
    print("RESULT acl_table_group_create_remove=PASS")




# A field the table did not enable should be rejected (known gap on some targets).
def test_poc_acl_entry_unknown_field_rejected(acl):
    """Check whether ACL fields are rejected when not enabled in the table."""

    candidates = [tuple(c) for c in _L1["unknown_field_candidates"]]

    empty_table = acl.create_table(
        fields=[],
        stage=_ACLP["stage"],
        bind_point=_ACLP["bind_point"],
    )

    accepted = []
    inconclusive = []

    try:
        for index, (field, value, mask) in enumerate(candidates):
            control_table = acl.create_table(
                fields=[field],
                stage=_ACLP["stage"],
                bind_point=_ACLP["bind_point"],
            )

            control_status, _ = acl.add_entry(
                control_table,
                _L1["unknown_field_control_priority_base"] + index,
                "SAI_PACKET_ACTION_DROP",
                [(field, value, mask)],
            )

            print(f"RESULT control_{field}={control_status}")

            if control_status != "SAI_STATUS_SUCCESS":
                inconclusive.append(field)
                continue

            status, _ = acl.add_entry(
                empty_table,
                _L1["unknown_field_experiment_priority_base"] + index,
                "SAI_PACKET_ACTION_DROP",
                [(field, value, mask)],
            )

            print(f"RESULT empty_table_{field}={status}")

            if status == "SAI_STATUS_SUCCESS":
                accepted.append(field)

        print(f"RESULT accepted_on_empty_table={accepted}")
        print(f"RESULT inconclusive_fields={inconclusive}")

        assert accepted == [], ("SAI accepted match fields on an ACL table with no match "
                                "fields enabled: " + ", ".join(accepted))

    finally:
        problems = acl.cleanup()
        assert problems == [], f"ACL cleanup failed: {problems}"


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

    # Set up VLAN and FDB so port 0 forwards to port 1.
    @pytest.mark.traffic
    @step("l2")
    def test_poc_acl_drop_01_l2_configured(self, npu, st):
        """Build the forwarding path (VLAN + FDB) the later steps send packets through."""
        need_traffic(npu)
        l2_setup(npu, st)

    # Before any ACL, TCP 80 and 81 must both get through.
    @pytest.mark.traffic
    @step("baseline", needs=["l2"])
    def test_poc_acl_drop_02_baseline_both_ports_forward(self, npu, dataplane, st):
        """Control: with NO ACL, TCP/80 and TCP/81 must both be forwarded."""
        need_traffic(npu)
        drop_ok = arrives(dataplane, DROP_PORT)
        fwd_ok = arrives(dataplane, FWD_PORT)
        print(f"RESULT baseline tcp{DROP_PORT}_forwarded={drop_ok} "
              f"tcp{FWD_PORT}_forwarded={fwd_ok}")
        assert drop_ok and fwd_ok, "control check failed: TCP does not pass the ports even without an ACL"

    # Create the ACL table.
    @step("table")
    def test_poc_acl_drop_03_table_created(self, npu, st):
        """Create the ACL table that will hold the drop and forward rules."""
        st.table = st.acl.create_table(BASIC_FIELDS)
        assert bool(st.table) and st.table != UNBOUND, f"ACL table create returned {st.table!r}"

    # Add the DROP (80) and FORWARD (81) entries.
    @step("entries", needs=["table"])
    def test_poc_acl_drop_04_entries_created(self, npu, st):
        """Add the two rules: DROP TCP/80 (priority 200), FORWARD TCP/81 (priority 100)."""
        status, drop_oid = st.acl.add_entry(
            st.table, DROP_PRIO, "SAI_PACKET_ACTION_DROP", tcp_match(DROP_PORT))
        assert status == OK, f"DROP entry create failed: {status}"
        status, fwd_oid = st.acl.add_entry(
            st.table, FWD_PRIO, "SAI_PACKET_ACTION_FORWARD", tcp_match(FWD_PORT))
        assert status == OK, f"FORWARD entry create failed: {status}"
        st.entries = [drop_oid, fwd_oid]

    # Bind the ACL to the ingress port.
    @step("bound", needs=["entries"])
    def test_poc_acl_drop_05_bound_to_ingress_port(self, npu, st):
        """Bind the ACL to the ingress port: from now on it filters what enters there."""
        acl_oid = st.table
        if BIND_MODE == "group":
            st.group = st.acl.create_group(st.table)
            acl_oid = st.group
        status = st.acl.bind_to_port(acl_oid, npu.port_oids[IN_IDX])
        assert status == OK, f"bind status: expected {OK!r}, got {status!r}"
        print(f"RESULT acl_bound mode={BIND_MODE} to_port_index={IN_IDX}")

    # TCP 80 must NOT come out of the egress port.
    @pytest.mark.traffic
    @step("drop_checked", needs=["baseline", "bound"])
    def test_poc_acl_drop_06_drop_port_is_dropped(self, npu, dataplane, st):
        """TCP/80 matches the DROP rule: it must NOT come out of the egress port."""
        need_traffic(npu)
        got = arrives(dataplane, DROP_PORT)
        print(f"RESULT tcp{DROP_PORT}_dropped={not got}")
        assert not got, f"TCP/{DROP_PORT} still came out of the egress port: DROP had no effect"

    # TCP 81 must come out of the egress port.
    @pytest.mark.traffic
    @step("fwd_checked", needs=["baseline", "bound"])
    def test_poc_acl_drop_07_fwd_port_is_forwarded(self, npu, dataplane, st):
        """TCP/81 matches the FORWARD rule: it must come out of the egress port."""
        need_traffic(npu)
        got = arrives(dataplane, FWD_PORT)
        print(f"RESULT tcp{FWD_PORT}_forwarded={got}")
        assert got, f"TCP/{FWD_PORT} did not arrive although a FORWARD entry exists"

    # TCP 82 matches no rule; result is printed, never judged.
    @pytest.mark.traffic
    @step("unmatched_checked", needs=["baseline", "bound"])
    def test_poc_acl_drop_08_unmatched_port_informational(self, npu, dataplane, st):
        """TCP/82 matches no rule: SAI says forward, some targets drop (implicit deny). Info only."""
        need_traffic(npu)
        got = arrives(dataplane, OTHER_PORT)
        print(f"RESULT tcp{OTHER_PORT}_unmatched_forwarded={got} "
              "(info only; False = implicit deny)")

    # The port's ingress ACL must read back as our table.
    @step("readback", needs=["bound"])
    def test_poc_acl_drop_09_port_acl_readback(self, npu, st):
        """Read the port's ingress ACL attribute: it must be what we bound."""
        bound = read_attr(npu, npu.port_oids[IN_IDX], PORT_ACL_ATTR, UNBOUND)
        expected = st.group or st.table
        assert bound == expected, f"port ACL readback: {bound}, expected {expected}"

    # Unbind the ACL from the port.
    @step("unbound", needs=["bound"])
    def test_poc_acl_drop_10_unbound(self, npu, st):
        """Unbind the ACL from the port (cleanup; forwarding is re-checked in step 13)."""
        status = st.acl.unbind_port(npu.port_oids[IN_IDX])
        assert status == OK, f"unbind status: expected {OK!r}, got {status!r}"

    # Remove the ACL entries.
    @step("entries_removed", needs=["entries", "unbound"])
    def test_poc_acl_drop_11_entries_removed(self, npu, st):
        """Remove both ACL entries (they must go before their table)."""
        for oid in reversed(st.entries):
            status = st.acl.remove_entry(oid)
            assert status == OK, f"remove entry {oid}: expected {OK!r}, got {status!r}"
        st.entries = []
        assert st.acl.entries == [], f"entries left behind: {st.acl.entries}"

    # Remove the ACL table.
    @step("table_removed", needs=["table", "entries_removed"])
    def test_poc_acl_drop_12_table_removed(self, npu, st):
        """Remove the table group (if used) and the table."""
        if st.group:
            st.acl.remove_group()
            st.group = None
        status = st.acl.remove_table(st.table)
        assert status == OK, f"remove table status: expected {OK!r}, got {status!r}"
        st.table = None
        assert st.acl.tables == [], f"tables left behind: {st.acl.tables}"

    # With the ACL gone, TCP 80 must pass again.
    @pytest.mark.traffic
    @step("after_removal", needs=["baseline", "table_removed"])
    def test_poc_acl_drop_13_forwarding_after_acl_removed(self, npu, dataplane, st):
        """With the ACL gone, TCP/80 and TCP/81 must be forwarded again."""
        need_traffic(npu)
        drop_ok = arrives(dataplane, DROP_PORT)
        fwd_ok = arrives(dataplane, FWD_PORT)
        print(f"RESULT after_acl_removal tcp{DROP_PORT}_forwarded={drop_ok} "
              f"tcp{FWD_PORT}_forwarded={fwd_ok}")
        assert drop_ok and fwd_ok, "L2 forwarding broke after the ACL was removed"

    # Remove VLAN/FDB and restore default VLAN and PVID.
    @step("l2_removed", needs=["l2"])
    def test_poc_acl_drop_14_l2_removed_and_defaults_restored(self, npu, st):
        """Undo the L2 setup: the switch is left as it was found."""
        problems = l2_teardown(npu, st)
        assert problems == [], problems


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

    # Set up VLAN and FDB so port 0 forwards to port 1.
    @pytest.mark.traffic
    @step("l2")
    def test_poc_acl_permit_priority_01_l2_configured(self, npu, st):
        """Build the forwarding path (VLAN + FDB) the later steps send packets through."""
        need_traffic(npu)
        l2_setup(npu, st)

    # Before any ACL, all three test ports must get through.
    @pytest.mark.traffic
    @step("baseline", needs=["l2"])
    def test_poc_acl_permit_priority_02_baseline_all_ports_forward(
            self, npu, dataplane, st):
        """Control: with NO ACL all three TCP ports must be forwarded."""
        need_traffic(npu)
        result = {p: arrives(dataplane, p)
                  for p in (PRIO_PORT, CTRL_DROP_PORT, REVERSE_PORT)}
        print(f"RESULT baseline forwarded={result}")
        assert all(result.values()), f"control check failed: TCP does not pass without an ACL: {result}"

    # Create the ACL table.
    @step("table")
    def test_poc_acl_permit_priority_03_table_created(self, npu, st):
        """Create the ACL table that will hold the five rules."""
        st.table = st.acl.create_table(BASIC_FIELDS)
        assert bool(st.table) and st.table != UNBOUND, f"ACL table create returned {st.table!r}"

    # Add the DROP and FORWARD entries with different priorities.
    @step("entries", needs=["table"])
    def test_poc_acl_permit_priority_04_entries_created(self, npu, st):
        """Add the five rules (bigger priority number = higher priority)."""
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

    # Bind the ACL to the ingress port.
    @step("bound", needs=["entries"])
    def test_poc_acl_permit_priority_05_bound_to_ingress_port(self, npu, st):
        """Bind the ACL to the ingress port."""
        acl_oid = st.table
        if BIND_MODE == "group":
            st.group = st.acl.create_group(st.table)
            acl_oid = st.group
        status = st.acl.bind_to_port(acl_oid, npu.port_oids[IN_IDX])
        assert status == OK, f"bind status: expected {OK!r}, got {status!r}"
        print(f"RESULT acl_bound mode={BIND_MODE} to_port_index={IN_IDX}")

    # FORWARD (higher number) beats DROP: the packet must arrive.
    @pytest.mark.traffic
    @step("permit_checked", needs=["baseline", "bound"])
    def test_poc_acl_permit_priority_06_permit_overrides_drop(self, npu, dataplane, st):
        """TCP/80 has DROP (low) and FORWARD (high): the higher-priority permit must win."""
        need_traffic(npu)
        got = arrives(dataplane, PRIO_PORT)
        print(f"RESULT tcp{PRIO_PORT}_permit_wins={got}")
        assert got, (f"TCP/{PRIO_PORT} was dropped although a higher-priority "
                     "FORWARD entry exists")

    # Control: a port with only a DROP entry must be dropped.
    @pytest.mark.traffic
    @step("control_checked", needs=["baseline", "bound"])
    def test_poc_acl_permit_priority_07_drop_without_permit_is_dropped(
            self, npu, dataplane, st):
        """TCP/81 has only a DROP: it must be dropped, which proves the DROP rules are live."""
        need_traffic(npu)
        got = arrives(dataplane, CTRL_DROP_PORT)
        print(f"RESULT tcp{CTRL_DROP_PORT}_dropped={not got}")
        assert not got, (f"TCP/{CTRL_DROP_PORT} still came out: the DROP entries do not "
                         "work in this table, so step 06 proves nothing")

    # Reverse case: higher-priority DROP beats FORWARD: must be dropped.
    @pytest.mark.traffic
    @step("reverse_checked", needs=["baseline", "bound"])
    def test_poc_acl_permit_priority_08_reverse_case_informational(
            self, npu, dataplane, st):
        """TCP/82 has FORWARD (low) and DROP (high): drop should win. Informational."""
        need_traffic(npu)
        got = arrives(dataplane, REVERSE_PORT)
        print(f"RESULT tcp{REVERSE_PORT}_dropped={not got} "
              "(info only; True = higher-priority DROP beat the lower FORWARD)")

    # Both entry priorities must read back as set.
    @step("readback", needs=["entries"])
    def test_poc_acl_permit_priority_09_priorities_read_back(self, npu, st):
        """Read the priorities of the TCP/80 DROP and FORWARD entries back from SAI."""
        low = read_attr(npu, st.entries[0], "SAI_ACL_ENTRY_ATTR_PRIORITY")
        high = read_attr(npu, st.entries[1], "SAI_ACL_ENTRY_ATTR_PRIORITY")
        assert (low, high) == (str(LOW_PRIO), str(HIGH_PRIO)), (
            f"priority readback: drop={low}, permit={high}")

    # Unbind the ACL from the port.
    @step("unbound", needs=["bound"])
    def test_poc_acl_permit_priority_10_unbound(self, npu, st):
        """Unbind the ACL from the port."""
        status = st.acl.unbind_port(npu.port_oids[IN_IDX])
        assert status == OK, f"unbind status: expected {OK!r}, got {status!r}"

    # Remove the ACL entries.
    @step("entries_removed", needs=["entries", "unbound"])
    def test_poc_acl_permit_priority_11_entries_removed(self, npu, st):
        """Remove the five entries (they must go before their table)."""
        for oid in reversed(st.entries):
            status = st.acl.remove_entry(oid)
            assert status == OK, f"remove entry {oid}: expected {OK!r}, got {status!r}"
        st.entries = []
        assert st.acl.entries == [], f"entries left behind: {st.acl.entries}"

    # Remove the ACL table.
    @step("table_removed", needs=["table", "entries_removed"])
    def test_poc_acl_permit_priority_12_table_removed(self, npu, st):
        """Remove the table group (if used) and the table."""
        if st.group:
            st.acl.remove_group()
            st.group = None
        status = st.acl.remove_table(st.table)
        assert status == OK, f"remove table status: expected {OK!r}, got {status!r}"
        st.table = None
        assert st.acl.tables == [], f"tables left behind: {st.acl.tables}"

    # With the ACL gone, all test ports must pass again.
    @pytest.mark.traffic
    @step("after_removal", needs=["baseline", "table_removed"])
    def test_poc_acl_permit_priority_13_forwarding_after_acl_removed(
            self, npu, dataplane, st):
        """With the ACL gone, all three TCP ports must be forwarded again."""
        need_traffic(npu)
        result = {p: arrives(dataplane, p)
                  for p in (PRIO_PORT, CTRL_DROP_PORT, REVERSE_PORT)}
        print(f"RESULT after_acl_removal forwarded={result}")
        assert all(result.values()), f"L2 forwarding broke after the ACL was removed: {result}"

    # Remove VLAN/FDB and restore default VLAN and PVID.
    @step("l2_removed", needs=["l2"])
    def test_poc_acl_permit_priority_14_l2_removed_and_defaults_restored(self, npu, st):
        """Undo the L2 setup: the switch is left as it was found."""
        problems = l2_teardown(npu, st)
        assert problems == [], problems
