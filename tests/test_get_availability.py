import pytest
from sai_client.sai_redis_client.sai_redis_client import SaiRedisClient
from saichallenger.common.sai_data import SaiObjType


@pytest.fixture(scope="module", autouse=True)
def skip_all(testbed_instance, npu):
    testbed = testbed_instance
    if testbed is not None and len(testbed.npu) != 1:
        pytest.skip(f"invalid for \"{testbed.name}\" testbed")
    
    if not isinstance(npu.sai_client, SaiRedisClient):
        pytest.skip("Get availability logic is not implemented for non-redis SAI client")

@pytest.fixture(autouse=True)
def on_prev_test_failure(prev_test_failed, npu):
    if prev_test_failed:
        npu.reset()


def _create_port_rif(npu, port_idx=0):
    npu.remove_vlan_member(npu.default_vlan_oid, npu.dot1q_bp_oids[port_idx])
    npu.remove(npu.dot1q_bp_oids[port_idx])
    return npu.create(
        SaiObjType.ROUTER_INTERFACE,
        [
            "SAI_ROUTER_INTERFACE_ATTR_TYPE", "SAI_ROUTER_INTERFACE_TYPE_PORT",
            "SAI_ROUTER_INTERFACE_ATTR_PORT_ID", npu.port_oids[port_idx],
            "SAI_ROUTER_INTERFACE_ATTR_VIRTUAL_ROUTER_ID", npu.default_vrf_oid,
        ],
    )


def _restore_port_l2(npu, port_idx=0):
    bp_oid = npu.create(
        SaiObjType.BRIDGE_PORT,
        [
            "SAI_BRIDGE_PORT_ATTR_TYPE", "SAI_BRIDGE_PORT_TYPE_PORT",
            "SAI_BRIDGE_PORT_ATTR_PORT_ID", npu.port_oids[port_idx],
            "SAI_BRIDGE_PORT_ATTR_ADMIN_STATE", "true",
        ],
    )
    npu.dot1q_bp_oids[port_idx] = bp_oid
    npu.create_vlan_member(npu.default_vlan_oid, bp_oid, "SAI_VLAN_TAGGING_MODE_UNTAGGED")
    npu.set(npu.port_oids[port_idx], ["SAI_PORT_ATTR_PORT_VLAN_ID", npu.default_vlan_id])


def test_next_hop_get_availability(npu):
    """
    Description:
    Get next-hop object availability, create a next hop, and verify availability decreases.

    Test scenario:
    1. Get next-hop availability with no extra attributes
    2. Create a next-hop object
    3. Get availability again and verify it decreased
    4. Clean up configuration
    """
    nhop_ip = "10.10.10.10"
    dst_mac = "00:99:99:99:99:99"

    status, before = npu.get_availability(npu.switch_oid, "SAI_OBJECT_TYPE_NEXT_HOP", do_assert=False)
    if (status == "SAI_STATUS_NOT_SUPPORTED"):
        pytest.skip("SAI_OBJECT_TYPE_NEXT_HOP availability is not supported")
    assert status == "SAI_STATUS_SUCCESS"

    rif = _create_port_rif(npu)
    neighbor_key = npu._neighbor_entry_key(rif, nhop_ip)
    npu.create(neighbor_key, ["SAI_NEIGHBOR_ENTRY_ATTR_DST_MAC_ADDRESS", dst_mac])
    nhop = npu.create(
        SaiObjType.NEXT_HOP,
        [
            "SAI_NEXT_HOP_ATTR_TYPE", "SAI_NEXT_HOP_TYPE_IP",
            "SAI_NEXT_HOP_ATTR_IP", nhop_ip,
            "SAI_NEXT_HOP_ATTR_ROUTER_INTERFACE_ID", rif,
        ],
    )

    after = npu.get_availability(npu.switch_oid, "SAI_OBJECT_TYPE_NEXT_HOP")
    assert after == before - 1

    npu.remove(nhop)
    npu.remove(neighbor_key)
    npu.remove(rif)
    _restore_port_l2(npu)

def test_neighbor_get_availability(npu):
    """
    Description:
    Get neighbor entry availability, create a neighbor, and verify availability decreases.

    Test scenario:
    1. Get neighbor entry availability with IPV4 address family
    2. Create a neighbor entry with IPV4 address family
    3. Get availability again and verify it decreased
    4. Clean up configuration
    """
    ipv4_addr = "10.10.10.1"
    mac_addr = "00:10:10:10:10:10"

    status, before = npu.get_availability(npu.switch_oid, "SAI_OBJECT_TYPE_NEIGHBOR_ENTRY", 
                                          ["SAI_NEIGHBOR_ENTRY_ATTR_IP_ADDR_FAMILY","SAI_IP_ADDR_FAMILY_IPV4"], 
                                          do_assert=False)
    if (status == "SAI_STATUS_NOT_SUPPORTED"):
        pytest.skip("SAI_OBJECT_TYPE_NEIGHBOR_ENTRY availability is not supported")
    assert status == "SAI_STATUS_SUCCESS"
    
    rif = _create_port_rif(npu)
    neighbor_key = npu._neighbor_entry_key(rif, ipv4_addr)
    npu.create(neighbor_key, ["SAI_NEIGHBOR_ENTRY_ATTR_DST_MAC_ADDRESS", mac_addr])

    after = npu.get_availability(npu.switch_oid, "SAI_OBJECT_TYPE_NEIGHBOR_ENTRY",
                                 ["SAI_NEIGHBOR_ENTRY_ATTR_IP_ADDR_FAMILY","SAI_IP_ADDR_FAMILY_IPV4"])
    assert after == before - 1

    npu.remove(neighbor_key)
    npu.remove(rif)
    _restore_port_l2(npu)