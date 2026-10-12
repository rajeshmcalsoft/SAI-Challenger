"""
Track B (L3 unicast routing) fixtures.

Each fixture creates what a test needs, hands it over, and undoes it in
reverse order afterwards, even if the test fails. The SAI calls are copied
from upstream: tests/ut/test_vrf_ut.py (VRF) and
tests/test_vrf.py::test_rif_create_remove (port RIF).
"""

import pytest
from saichallenger.common.sai_data import SaiObjType

# Index of the port used for the RIF (into npu.port_oids / npu.dot1q_bp_oids)
PORT_IDX = 0


@pytest.fixture
def vrf(npu):
    """Create a non-default VRF, yield its OID, remove it afterwards."""
    vrf_oid = npu.create(SaiObjType.VIRTUAL_ROUTER, [])
    assert vrf_oid != "oid:0x0"
    try:
        yield vrf_oid
    finally:
        npu.remove(vrf_oid)


@pytest.fixture
def port_rif(npu, vrf):
    """
    Turn port PORT_IDX into a routed port in the `vrf` VRF and yield the RIF OID.
    Afterwards the port is put back into the default VLAN.
    """
    port_oid = npu.port_oids[PORT_IDX]
    bp_oid = npu.dot1q_bp_oids[PORT_IDX]

    vlan_member_removed = False
    bridge_port_removed = False
    rif_oid = None

    try:
        # A port must leave the default VLAN and its bridge port before it can have a RIF
        npu.remove_vlan_member(npu.default_vlan_oid, bp_oid)
        vlan_member_removed = True

        npu.remove(bp_oid)
        bridge_port_removed = True

        rif_oid = npu.create(
            SaiObjType.ROUTER_INTERFACE,
            [
                "SAI_ROUTER_INTERFACE_ATTR_TYPE", "SAI_ROUTER_INTERFACE_TYPE_PORT",
                "SAI_ROUTER_INTERFACE_ATTR_PORT_ID", port_oid,
                "SAI_ROUTER_INTERFACE_ATTR_VIRTUAL_ROUTER_ID", vrf,
            ],
        )
        yield rif_oid
    finally:
        # Undo only the steps that actually happened, in reverse order
        if rif_oid is not None:
            npu.remove(rif_oid)

        if bridge_port_removed:
            new_bp_oid = npu.create(
                SaiObjType.BRIDGE_PORT,
                [
                    "SAI_BRIDGE_PORT_ATTR_TYPE", "SAI_BRIDGE_PORT_TYPE_PORT",
                    "SAI_BRIDGE_PORT_ATTR_PORT_ID", port_oid,
                    "SAI_BRIDGE_PORT_ATTR_ADMIN_STATE", "true",
                ],
            )
            # Keep the npu's bridge port list in sync with the new bridge port
            npu.dot1q_bp_oids[PORT_IDX] = new_bp_oid
            bp_oid = new_bp_oid

        if vlan_member_removed:
            npu.create_vlan_member(
                npu.default_vlan_oid, bp_oid, "SAI_VLAN_TAGGING_MODE_UNTAGGED"
            )
            npu.set(port_oid, ["SAI_PORT_ATTR_PORT_VLAN_ID", npu.default_vlan_id])
