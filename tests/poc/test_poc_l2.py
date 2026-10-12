"""Track A, Level 1 (control plane): VLAN read-back tests. Run on saivs and saivpp."""
import pytest

from .lib import l2

pytestmark = pytest.mark.l2

VLAN_ID = "20"


def test_poc_l2_vlan_readback(npu):
    """VLAN 20 is created and SAI_VLAN_ATTR_VLAN_ID reads back as 20."""
    with l2.vlan(npu, VLAN_ID) as vlan_oid:
        data = npu.get(vlan_oid, ["SAI_VLAN_ATTR_VLAN_ID"])
        assert data.value() == VLAN_ID


def test_poc_l2_vlan_member_readback(npu):
    """A tagged member reads back SAI_VLAN_TAGGING_MODE_TAGGED on the right bridge port."""
    with l2.vlan(npu, VLAN_ID) as vlan_oid, \
            l2.vlan_member(npu, vlan_oid, 0, l2.TAGGED) as member_oid:
        mode = npu.get(member_oid, ["SAI_VLAN_MEMBER_ATTR_VLAN_TAGGING_MODE"]).value()
        assert mode == l2.TAGGED
        bp_oid = npu.get(member_oid, ["SAI_VLAN_MEMBER_ATTR_BRIDGE_PORT_ID"]).oid()
        assert bp_oid == npu.dot1q_bp_oids[0]
        members = npu.get(vlan_oid, ["SAI_VLAN_ATTR_MEMBER_LIST"]).to_list()
        assert member_oid in members
