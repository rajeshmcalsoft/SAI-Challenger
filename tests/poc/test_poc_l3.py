# ruff: noqa: F811  (imported pytest fixtures are used as test arguments)
"""Track B (L3 unicast routing): Level 1 (control plane) tests for VRF and port RIF."""

import pytest

from .lib.l3 import neighbor_nexthop, next_hop_group, port_rif, vrf  # noqa: F401  (pytest fixtures)

pytestmark = pytest.mark.l3

# Values taken from upstream tests/native/test_route.py and tests/test_dc_t1.py
NEIGHBOR_IP = "10.10.10.2"
NEIGHBOR_MAC = "00:11:22:33:44:55"
# Second neighbor, as in upstream test_route_update
NEIGHBOR_IP_2 = "10.10.10.3"
NEIGHBOR_MAC_2 = "00:11:22:33:44:66"
ROUTE_V4 = "100.0.0.0/8"
# SUGGESTION (not from the plans or upstream): IPv6 documentation prefix (RFC 3849)
ROUTE_V6 = "2001:db8::/32"
PACKET_ACTION = "SAI_ROUTE_ENTRY_ATTR_PACKET_ACTION"
FORWARD = "SAI_PACKET_ACTION_FORWARD"
DROP = "SAI_PACKET_ACTION_DROP"
NEXT_HOP_ID = "SAI_ROUTE_ENTRY_ATTR_NEXT_HOP_ID"


def test_poc_l3_vrf_default_admin_state(npu, vrf):
    # A new (non-default) VRF has admin state "true" by default
    # (see TestNonDefaultVrf in tests/ut/test_vrf_ut.py).
    # The expected result follows the SAI specification. A target that cannot
    # read a never-set attribute (saivs: SAI_STATUS_NOT_IMPLEMENTED) is recorded
    # in its profile under expected_gaps, not here.
    state = npu.get(vrf, ["SAI_VIRTUAL_ROUTER_ATTR_ADMIN_V4_STATE"]).value()
    assert state == "true"


def test_poc_l3_vrf_create_readback(npu, vrf):
    # Follows tests/test_vrf.py::test_default_vrf: set the attribute first, then
    # read it back. "false" differs from the default, so the read-back proves the
    # set worked.
    npu.set(vrf, ["SAI_VIRTUAL_ROUTER_ATTR_ADMIN_V4_STATE", "false"])
    state = npu.get(vrf, ["SAI_VIRTUAL_ROUTER_ATTR_ADMIN_V4_STATE", "true"]).to_json()
    assert state[1].lower() == "false"


def test_poc_l3_port_rif_create_remove(npu, port_rif):
    rif_type = npu.get(port_rif, ["SAI_ROUTER_INTERFACE_ATTR_TYPE"]).value()
    assert rif_type == "SAI_ROUTER_INTERFACE_TYPE_PORT"


def test_poc_l3_port_rif_port_restored(npu, poc_ports):
    # Must run right after the RIF test (pytest keeps file order): its fixture
    # is already torn down, so the port should be back in the default VLAN.
    # get_vlan_member() returns None if it is not.
    port_idx = poc_ports(1)[0]
    bp_oid = npu.dot1q_bp_oids[port_idx]
    assert npu.get_vlan_member(npu.default_vlan_oid, bp_oid) is not None


def test_poc_l3_neighbor_nexthop_create_remove(npu, port_rif, neighbor_nexthop):
    # port_rif is listed before neighbor_nexthop so the RIF is torn down last.
    neighbor_key, nexthop_oid = neighbor_nexthop.create(port_rif, NEIGHBOR_IP, NEIGHBOR_MAC)

    # Keys are read with the two-element form, as in upstream test_route.py.
    # The second element is a placeholder of the attribute's type (a MAC here).
    mac = npu.get(neighbor_key, ["SAI_NEIGHBOR_ENTRY_ATTR_DST_MAC_ADDRESS", "00:00:00:00:00:00"]).value()
    assert mac.lower() == NEIGHBOR_MAC

    assert npu.get(nexthop_oid, ["SAI_NEXT_HOP_ATTR_TYPE"]).value() == "SAI_NEXT_HOP_TYPE_IP"
    assert npu.get(nexthop_oid, ["SAI_NEXT_HOP_ATTR_ROUTER_INTERFACE_ID"]).oid() == port_rif

    # The next hop uses the neighbor, so it is removed first
    neighbor_nexthop.remove(nexthop_oid)
    neighbor_nexthop.remove(neighbor_key)


def _cpu_port_oid(npu):
    return npu.get(npu.switch_oid, ["SAI_SWITCH_ATTR_CPU_PORT", "oid:0x0"]).oid()


def _route_create_remove(npu, vrf, prefix):
    # Same call pattern as upstream test_dc_t1.py::test_basic_route: a route whose
    # next hop is the CPU port, with an explicit packet action.
    cpu_port_oid = _cpu_port_oid(npu)
    npu.create_route(prefix, vrf, cpu_port_oid, [PACKET_ACTION, FORWARD])
    try:
        route_key = npu._route_entry_key(vrf, prefix)
        action = npu.get(route_key, [PACKET_ACTION, ""]).value()
        assert action == FORWARD
        next_hop = npu.get(route_key, [NEXT_HOP_ID, "oid:0x0"]).oid()
        assert next_hop == cpu_port_oid
    finally:
        npu.remove_route(prefix, vrf)


def test_poc_l3_route_ipv4_create_remove(npu, vrf):
    _route_create_remove(npu, vrf, ROUTE_V4)


def test_poc_l3_route_ipv6_create_remove(npu, vrf):
    _route_create_remove(npu, vrf, ROUTE_V6)


def test_poc_l3_nhg_create_remove(npu, port_rif, neighbor_nexthop, next_hop_group):
    # Fixture order matters for teardown: group and members first, then the
    # next hops and neighbors, then the RIF.
    _, nexthop_1 = neighbor_nexthop.create(port_rif, NEIGHBOR_IP, NEIGHBOR_MAC)
    _, nexthop_2 = neighbor_nexthop.create(port_rif, NEIGHBOR_IP_2, NEIGHBOR_MAC_2)

    group_oid = next_hop_group.create()
    member_1 = next_hop_group.add_member(group_oid, nexthop_1)
    member_2 = next_hop_group.add_member(group_oid, nexthop_2)

    for member, nexthop in ((member_1, nexthop_1), (member_2, nexthop_2)):
        group_attr = "SAI_NEXT_HOP_GROUP_MEMBER_ATTR_NEXT_HOP_GROUP_ID"
        nexthop_attr = "SAI_NEXT_HOP_GROUP_MEMBER_ATTR_NEXT_HOP_ID"
        assert npu.get(member, [group_attr]).oid() == group_oid
        assert npu.get(member, [nexthop_attr]).oid() == nexthop

    # Members go before the group they belong to
    next_hop_group.remove(member_2)
    next_hop_group.remove(member_1)
    next_hop_group.remove(group_oid)


def test_poc_l3_route_attr_update(npu, vrf):
    npu.create_route(ROUTE_V4, vrf, _cpu_port_oid(npu), [PACKET_ACTION, FORWARD])
    try:
        route_key = npu._route_entry_key(vrf, ROUTE_V4)
        npu.set(route_key, [PACKET_ACTION, DROP])
        action = npu.get(route_key, [PACKET_ACTION, ""]).value()
        assert action == DROP
    finally:
        npu.remove_route(ROUTE_V4, vrf)


def test_poc_l3_route_nexthop_change(npu, vrf, port_rif, neighbor_nexthop):
    # Same sequence as upstream test_route_update, but without packets (Level 1).
    # The route must be removed in the test: it uses the next hops that
    # neighbor_nexthop removes at teardown.
    _, nexthop_1 = neighbor_nexthop.create(port_rif, NEIGHBOR_IP, NEIGHBOR_MAC)
    _, nexthop_2 = neighbor_nexthop.create(port_rif, NEIGHBOR_IP_2, NEIGHBOR_MAC_2)

    npu.create_route(ROUTE_V4, vrf, nexthop_1)
    try:
        route_key = npu._route_entry_key(vrf, ROUTE_V4)
        assert npu.get(route_key, [NEXT_HOP_ID, "oid:0x0"]).oid() == nexthop_1

        npu.set(route_key, [NEXT_HOP_ID, nexthop_2])
        assert npu.get(route_key, [NEXT_HOP_ID, "oid:0x0"]).oid() == nexthop_2
    finally:
        npu.remove_route(ROUTE_V4, vrf)
