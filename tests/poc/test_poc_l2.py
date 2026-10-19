"""Track A, Level 1 (control plane): VLAN and FDB tests.

Expected results follow the SAI specification. Where a target deviates, the
deviation is an expected gap in tests/poc/targets/<profile>.json, not in this file.
"""
import pytest
from saichallenger.common.sai_data import SaiObjType

from .lib import l2

pytestmark = pytest.mark.l2

VLAN_ID = "20"
MAC = "00:11:11:11:11:11"
SUCCESS = "SAI_STATUS_SUCCESS"
EXISTS = "SAI_STATUS_ITEM_ALREADY_EXISTS"


def test_poc_l2_vlan_readback(npu):
    """VLAN 20 is created and SAI_VLAN_ATTR_VLAN_ID reads back as 20."""
    with l2.vlan(npu, VLAN_ID) as vlan_oid:
        data = npu.get(vlan_oid, ["SAI_VLAN_ATTR_VLAN_ID"])
        assert data.value() == VLAN_ID


def test_poc_l2_vlan_member_readback(npu, poc_ports):
    """A tagged member reads back SAI_VLAN_TAGGING_MODE_TAGGED on the right bridge port."""
    (p,) = poc_ports(1)
    with l2.vlan(npu, VLAN_ID) as vlan_oid, \
            l2.vlan_member(npu, vlan_oid, p, l2.TAGGED) as member_oid:
        mode = npu.get(member_oid, ["SAI_VLAN_MEMBER_ATTR_VLAN_TAGGING_MODE"]).value()
        assert mode == l2.TAGGED
        bp_oid = npu.get(member_oid, ["SAI_VLAN_MEMBER_ATTR_BRIDGE_PORT_ID"]).oid()
        assert bp_oid == npu.dot1q_bp_oids[p]
        members = npu.get(vlan_oid, ["SAI_VLAN_ATTR_MEMBER_LIST"]).to_list()
        assert member_oid in members


def test_poc_l2_fdb_static_readback(npu, poc_ports):
    """A static FDB entry reads back with the right bridge port."""
    (p,) = poc_ports(1)
    with l2.vlan(npu, VLAN_ID) as vlan_oid, \
            l2.vlan_member(npu, vlan_oid, p), \
            l2.static_fdb(npu, vlan_oid, MAC, p):
        key = npu.fdb_entry_key(vlan_oid, MAC)
        bp_oid = npu.get(key, ["SAI_FDB_ENTRY_ATTR_BRIDGE_PORT_ID", "oid:0x0"]).oid()
        assert bp_oid == npu.dot1q_bp_oids[p]


@pytest.mark.negative
def test_poc_l2_vlan_duplicate_rejected(npu):
    """Creating VLAN 20 twice fails with SAI_STATUS_ITEM_ALREADY_EXISTS."""
    with l2.vlan(npu, VLAN_ID):
        status, dup_oid = npu.create(
            SaiObjType.VLAN, ["SAI_VLAN_ATTR_VLAN_ID", VLAN_ID], do_assert=False)
        try:
            assert status == EXISTS, f"duplicate VLAN {VLAN_ID} returned {status}"
        finally:
            if status == SUCCESS:
                npu.remove(dup_oid)


@pytest.mark.negative
@pytest.mark.parametrize("bad_id", ["0", "4095", "4096"])
def test_poc_l2_vlan_invalid_id_rejected(npu, bad_id):
    """Creating a VLAN with ID 0, 4095 or 4096 fails."""
    status, vlan_oid = npu.create(
        SaiObjType.VLAN, ["SAI_VLAN_ATTR_VLAN_ID", bad_id], do_assert=False)
    try:
        assert status != SUCCESS, f"VLAN ID {bad_id} was accepted"
    finally:
        if status == SUCCESS:
            npu.remove(vlan_oid)
