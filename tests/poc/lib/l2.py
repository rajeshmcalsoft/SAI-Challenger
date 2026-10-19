"""Track A helpers: self-cleaning VLAN, VLAN member and static FDB context managers.

Every helper undoes what it did, even when the body raises, so a failed check
never leaves the switch dirty. Cleanup order mirrors upstream
test_l2_access_to_access_vlan: member -> default-VLAN member -> PVID.
"""
from contextlib import ExitStack, contextmanager

from saichallenger.common.sai_data import SaiObjType

UNTAGGED = "SAI_VLAN_TAGGING_MODE_UNTAGGED"
TAGGED = "SAI_VLAN_TAGGING_MODE_TAGGED"
PVID = "SAI_PORT_ATTR_PORT_VLAN_ID"


@contextmanager
def vlan(npu, vlan_id):
    """Create VLAN <vlan_id>; yield its OID; remove it on exit."""
    vlan_oid = npu.create(SaiObjType.VLAN, ["SAI_VLAN_ATTR_VLAN_ID", str(vlan_id)])
    try:
        yield vlan_oid
    finally:
        npu.remove(vlan_oid)


@contextmanager
def vlan_member(npu, vlan_oid, port_idx, tagging=UNTAGGED):
    """Add port <port_idx> to <vlan_oid>; yield the member OID.

    UNTAGGED (access): the port leaves the default VLAN and gets PVID = VLAN ID.
    TAGGED (trunk): the port stays in the default VLAN, as in upstream test_l2_basic.py.
    Everything is restored on exit.
    """
    bp_oid = npu.dot1q_bp_oids[port_idx]
    port_oid = npu.port_oids[port_idx]
    vlan_id = npu.get(vlan_oid, ["SAI_VLAN_ATTR_VLAN_ID"]).value()
    with ExitStack() as undo:  # callbacks run last-registered-first
        if tagging == UNTAGGED:
            undo.callback(npu.set, port_oid, [PVID, npu.default_vlan_id])  # runs last
            npu.remove_vlan_member(npu.default_vlan_oid, bp_oid)
            undo.callback(npu.create_vlan_member, npu.default_vlan_oid, bp_oid, UNTAGGED)
        member_oid = npu.create_vlan_member(vlan_oid, bp_oid, tagging)
        undo.callback(npu.remove, member_oid)  # runs first
        if tagging == UNTAGGED:
            npu.set(port_oid, [PVID, vlan_id])
        yield member_oid


@contextmanager
def static_fdb(npu, vlan_oid, mac, port_idx):
    """Create a static FDB entry <mac> -> port <port_idx> in <vlan_oid>; remove it on exit."""
    bp_oid = npu.dot1q_bp_oids[port_idx]
    npu.create_fdb(vlan_oid, mac, bp_oid)
    try:
        yield bp_oid
    finally:
        npu.remove_fdb(vlan_oid, mac)


@contextmanager
def flush_on_exit(npu, vlan_oid):
    """Flush every FDB entry of the VLAN on exit (learned MACs would break the next test).

    Put it after the members in the with-statement so it runs before they are removed,
    as upstream test_l2_vlan_bcast_ucast does.
    """
    try:
        yield
    finally:
        npu.flush_fdb_entries(npu.switch_oid, [
            "SAI_FDB_FLUSH_ATTR_BV_ID", vlan_oid,
            "SAI_FDB_FLUSH_ATTR_ENTRY_TYPE", "SAI_FDB_FLUSH_ENTRY_TYPE_ALL"])


@contextmanager
def access_vlan(npu, vlan_id, port_macs):
    """VLAN + untagged members + static FDB for {port_idx: mac}; yield the VLAN OID."""
    with ExitStack() as stack:
        vlan_oid = stack.enter_context(vlan(npu, vlan_id))
        for idx, mac in port_macs.items():
            stack.enter_context(vlan_member(npu, vlan_oid, idx))
            stack.enter_context(static_fdb(npu, vlan_oid, mac, idx))
        yield vlan_oid


# ---- stray-frame filter (MLD) -------------------------------------------------
MLD_TYPES = frozenset({130, 131, 132, 143})  # query, v1 report, v1 done, v2 report
_VLAN_TPIDS = (0x8100, 0x88A8)
_IPV6_EXT_HDRS = (0, 43, 60)  # hop-by-hop, routing, destination options


def is_mld(frame):
    """True if the raw Ethernet frame is an MLD message (ICMPv6 type 130/131/132/143)."""
    off = 12
    ethertype = int.from_bytes(frame[off:off + 2], "big")
    while ethertype in _VLAN_TPIDS:
        off += 4
        ethertype = int.from_bytes(frame[off:off + 2], "big")
    if ethertype != 0x86DD:
        return False
    pos = off + 2 + 40
    if len(frame) < pos:
        return False
    nxt = frame[off + 2 + 6]
    while nxt in _IPV6_EXT_HDRS:
        if len(frame) < pos + 8:
            return False
        nxt = frame[pos]
        pos += (frame[pos + 1] + 1) * 8
    return nxt == 58 and len(frame) > pos and frame[pos] in MLD_TYPES


def not_mld_filter(frame):
    """PTF filter: keep the frame (True) unless it is MLD."""
    return not is_mld(frame)


def install_mld_filter():
    """Make every PTF poll in this process skip MLD frames (idempotent)."""
    from ptf import testutils
    if not_mld_filter not in testutils.get_filters():
        testutils.add_filter(not_mld_filter)


def remove_mld_filter():
    # ptf.testutils.reset_filters() is a no-op in ptf 0.12 (missing 'global'), so edit the list.
    from ptf import testutils
    if not_mld_filter in testutils.get_filters():
        testutils.get_filters().remove(not_mld_filter)
