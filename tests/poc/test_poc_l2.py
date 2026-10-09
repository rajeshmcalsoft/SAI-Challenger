"""Track A (L2 forwarding and VLANs) tests.

Level 1 = API read-back, works on saivs and saivpp.
Imports are relative because this folder is mounted as tests/poc_<name>.
"""
import pytest

from .lib import l2

pytestmark = pytest.mark.l2


@pytest.fixture
def vlan20(npu):
    """VLAN 20, removed after the test."""
    with l2.vlan(npu) as vlan_oid:
        yield vlan_oid


def test_poc_l2_vlan_readback(npu):
    """VLAN 20 is created and SAI_VLAN_ATTR_VLAN_ID reads back as 20."""
    with l2.vlan(npu, l2.TEST_VLAN_ID) as vlan_oid:
        got = npu.get(vlan_oid, ["SAI_VLAN_ATTR_VLAN_ID"]).value()
        assert got == l2.TEST_VLAN_ID, f"VLAN id read back as {got}"


def test_poc_l2_vlan_member_readback(npu, vlan20):
    """A tagged member reads back SAI_VLAN_TAGGING_MODE_TAGGED."""
    with l2.vlan_member(npu, vlan20, 0, l2.TAGGED) as mbr_oid:
        mode = npu.get(mbr_oid, ["SAI_VLAN_MEMBER_ATTR_VLAN_TAGGING_MODE"]).value()
        assert mode == l2.TAGGED, f"tagging mode read back as {mode}"

        bp_oid = npu.get(mbr_oid, ["SAI_VLAN_MEMBER_ATTR_BRIDGE_PORT_ID"]).value()
        assert bp_oid == npu.dot1q_bp_oids[0], f"bridge port read back as {bp_oid}"

        members = npu.get(vlan20, ["SAI_VLAN_ATTR_MEMBER_LIST"]).to_list()
        assert mbr_oid in members, "member missing from SAI_VLAN_ATTR_MEMBER_LIST"
