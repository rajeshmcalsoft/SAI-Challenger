"""Track A (L2 forwarding and VLANs) helpers.

Each helper is a context manager that undoes what it did, in reverse order,
even when the test body raises. Always read the oid lists from `npu` at call
time: npu.reset() rebuilds them after a failed test.
"""
from contextlib import ExitStack, contextmanager

from saichallenger.common.sai_data import SaiObjType

TAGGED = "SAI_VLAN_TAGGING_MODE_TAGGED"
UNTAGGED = "SAI_VLAN_TAGGING_MODE_UNTAGGED"
TEST_VLAN_ID = "20"


@contextmanager
def vlan(npu, vlan_id=TEST_VLAN_ID):
    """Create a VLAN, yield its oid, remove it on exit."""
    oid = npu.create(SaiObjType.VLAN, ["SAI_VLAN_ATTR_VLAN_ID", str(vlan_id)])
    try:
        yield oid
    finally:
        npu.remove(oid)


@contextmanager
def vlan_member(npu, vlan_oid, port_idx, tagging_mode=UNTAGGED):
    """Move port `port_idx` from the default VLAN into `vlan_oid`.

    Yields the VLAN member oid. For an untagged member the port PVID is set
    to the VLAN id. On exit (reverse order): PVID back to the default, member
    removed, port back in the default VLAN as untagged.
    """
    bp_oid = npu.dot1q_bp_oids[port_idx]
    port_oid = npu.port_oids[port_idx]
    vlan_id = npu.get(vlan_oid, ["SAI_VLAN_ATTR_VLAN_ID"]).value()
    pvid = ["SAI_PORT_ATTR_PORT_VLAN_ID"]

    with ExitStack() as undo:
        npu.remove_vlan_member(npu.default_vlan_oid, bp_oid)
        undo.callback(npu.create_vlan_member, npu.default_vlan_oid, bp_oid, UNTAGGED)

        mbr_oid = npu.create_vlan_member(vlan_oid, bp_oid, tagging_mode)
        undo.callback(npu.remove, mbr_oid)

        if tagging_mode == UNTAGGED:
            npu.set(port_oid, pvid + [vlan_id])
            undo.callback(npu.set, port_oid, pvid + [npu.default_vlan_id])

        yield mbr_oid
