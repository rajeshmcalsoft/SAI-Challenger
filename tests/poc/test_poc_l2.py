"""Track A, Level 1 (control plane): VLAN and FDB tests.

Expected results follow the SAI specification. Where a target deviates, the
deviation is an expected gap in tests/poc/targets/<profile>.json, not in this file.
"""
import pytest
from saichallenger.common.sai_data import SaiObjType

from .lib import data, l2

pytestmark = pytest.mark.l2

D = data.load("l2")  # test-chosen values: tests/poc/data/l2.json
l2.check_data(D)
VLAN_ID = str(D.vlan_id)
OTHER_VLAN_ID = str(D.second_vlan_id)
MAC = D.macs[0]

# defined by the SAI spec, so they stay here
SUCCESS = "SAI_STATUS_SUCCESS"
EXISTS = "SAI_STATUS_ITEM_ALREADY_EXISTS"
IN_USE = "SAI_STATUS_OBJECT_IN_USE"


def test_poc_l2_vlan_readback(npu):
    """The test VLAN is created and SAI_VLAN_ATTR_VLAN_ID reads back as its ID."""
    with l2.vlan(npu, VLAN_ID) as vlan_oid:
        attr = npu.get(vlan_oid, ["SAI_VLAN_ATTR_VLAN_ID"])
        assert attr.value() == VLAN_ID


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
    """Creating the test VLAN twice fails with SAI_STATUS_ITEM_ALREADY_EXISTS."""
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


@pytest.mark.negative
def test_poc_l2_remove_vlan_with_members_rejected(npu, poc_ports):
    """Removing a VLAN that still has a member fails with OBJECT_IN_USE; the VLAN stays."""
    (p,) = poc_ports(1)
    other_vlan = npu.create(SaiObjType.VLAN, ["SAI_VLAN_ATTR_VLAN_ID", OTHER_VLAN_ID])
    member = npu.create_vlan_member(other_vlan, npu.dot1q_bp_oids[p], l2.TAGGED)
    status = npu.remove(other_vlan, do_assert=False)
    try:
        assert status == IN_USE, f"target removes a VLAN that has members: {status}"
        assert npu.get(other_vlan, ["SAI_VLAN_ATTR_VLAN_ID"]).value() == OTHER_VLAN_ID
    finally:
        if status == SUCCESS:
            npu.reset()  # the VLAN is gone but its member may remain; tell the team channel
        else:
            npu.remove(member)
            npu.remove(other_vlan)


@pytest.mark.negative
def test_poc_l2_remove_bridge_port_in_use_rejected(npu, poc_ports):
    """Removing a bridge port that is a VLAN member fails with OBJECT_IN_USE; the port stays."""
    (p,) = poc_ports(1)
    bp_oid = npu.dot1q_bp_oids[p]
    other_vlan = npu.create(SaiObjType.VLAN, ["SAI_VLAN_ATTR_VLAN_ID", OTHER_VLAN_ID])
    member = npu.create_vlan_member(other_vlan, bp_oid, l2.TAGGED)
    status = npu.remove(bp_oid, do_assert=False)
    try:
        assert status == IN_USE, f"target removes a bridge port that is in use: {status}"
    finally:
        if status == SUCCESS:
            npu.reset()  # the default bridge port is gone; tell the team channel
        else:
            npu.remove(member)
            npu.remove(other_vlan)
