# ruff: noqa: F811  (imported pytest fixtures are used as test arguments)
"""Track B (L3 unicast routing): Level 1 (control plane) tests for VRF and port RIF."""

import pytest
from saichallenger.common.sai_data import SaiObjType

from .lib.l3 import neighbor_nexthop, next_hop_group, port_rif, vlan_rif, vrf  # noqa: F401  (pytest fixtures)

pytestmark = pytest.mark.l3

# Values taken from upstream tests/native/test_route.py and tests/test_dc_t1.py
NEIGHBOR_IP = "10.10.10.2"
NEIGHBOR_MAC = "00:11:22:33:44:55"
# Second neighbor, as in upstream test_route_update
NEIGHBOR_IP_2 = "10.10.10.3"
NEIGHBOR_MAC_2 = "00:11:22:33:44:66"
# SUGGESTION (not from the plans or upstream): IPv6 documentation address (RFC 3849)
NEIGHBOR_IP_V6 = "2001:db8::2"
ROUTE_V4 = "100.0.0.0/8"
# SUGGESTION (not from the plans or upstream): IPv6 documentation prefix (RFC 3849)
ROUTE_V6 = "2001:db8::/32"
# Edge-case prefixes (plan, Week 3, Wed 21 Oct); IPv6 host address from RFC 3849
ROUTE_V4_HOST = "10.10.10.1/32"
ROUTE_V6_HOST = "2001:db8::1/128"
ROUTE_V4_DEFAULT = "0.0.0.0/0"
PACKET_ACTION = "SAI_ROUTE_ENTRY_ATTR_PACKET_ACTION"
FORWARD = "SAI_PACKET_ACTION_FORWARD"
DROP = "SAI_PACKET_ACTION_DROP"
NEXT_HOP_ID = "SAI_ROUTE_ENTRY_ATTR_NEXT_HOP_ID"
SUCCESS = "SAI_STATUS_SUCCESS"


def test_poc_l3_vrf_default_admin_state(npu, vrf):
    # A new (non-default) VRF has admin state "true" by default
    # (see TestNonDefaultVrf in tests/ut/test_vrf_ut.py).
    # The expected result follows the SAI specification. A target that cannot
    # read a never-set attribute (saivs: SAI_STATUS_NOT_IMPLEMENTED,
    # saivpp: SAI_STATUS_ITEM_NOT_FOUND) is recorded in its profile under
    # expected_gaps, not here. Verified on saivs, saivpp and saivs-thrift
    # on 21 Oct 2026; all three appear as strict xfail via the profile.
    state = npu.get(vrf, ["SAI_VIRTUAL_ROUTER_ATTR_ADMIN_V4_STATE"]).value()
    assert state == "true"


def test_poc_l3_vrf_create_readback(npu, vrf):
    # Follows tests/test_vrf.py::test_default_vrf: set the attribute first, then
    # read it back. "false" differs from the SAI default (saivirtualrouter.h:
    # @default true), so reading "false" back proves the set worked. Setting
    # "true" would not: it could be read from the default without the set taking
    # effect.
    npu.set(vrf, ["SAI_VIRTUAL_ROUTER_ATTR_ADMIN_V4_STATE", "false"])
    state = npu.get(vrf, ["SAI_VIRTUAL_ROUTER_ATTR_ADMIN_V4_STATE", "true"]).value()
    assert state == "false"


def test_poc_l3_port_rif_create_remove(npu, port_rif):
    rif_type = npu.get(port_rif, ["SAI_ROUTER_INTERFACE_ATTR_TYPE"]).value()
    assert rif_type == "SAI_ROUTER_INTERFACE_TYPE_PORT"


def test_poc_l3_port_rif_port_restored(npu, poc_ports):
    # Must run right after the RIF test (pytest keeps file order): its fixture
    # is already torn down, so the port should be back in the default VLAN.
    # poc_ports(n) returns the first n profile port indices (confirmed in
    # tests/poc/conftest.py); get_vlan_member() returns None if it is not a member.
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


def _status(result):
    # npu.create(key, attrs, False) returns (status, vid) on both the redis
    # client (sai_redis_client.py) and the thrift client (sai_thrift_client.py).
    # A plain status string is also accepted for safety.
    return result[0] if isinstance(result, tuple) else result


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


def test_poc_l3_route_ipv4_host_create_remove(npu, vrf):
    # Edge-case: /32 host route (plan, Week 3, Wed 21 Oct).
    _route_create_remove(npu, vrf, ROUTE_V4_HOST)


def test_poc_l3_route_ipv6_host_create_remove(npu, vrf):
    # Edge-case: /128 host route (plan, Week 3, Wed 21 Oct).
    _route_create_remove(npu, vrf, ROUTE_V6_HOST)


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
    # Model: upstream test_route_update (without traffic). Full round trip:
    # set DROP and read it back, then restore FORWARD and read it back.
    npu.create_route(ROUTE_V4, vrf, _cpu_port_oid(npu), [PACKET_ACTION, FORWARD])
    try:
        route_key = npu._route_entry_key(vrf, ROUTE_V4)
        npu.set(route_key, [PACKET_ACTION, DROP])
        assert npu.get(route_key, [PACKET_ACTION, ""]).value() == DROP
        npu.set(route_key, [PACKET_ACTION, FORWARD])
        assert npu.get(route_key, [PACKET_ACTION, ""]).value() == FORWARD
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


def test_poc_l3_vlan_rif_create_remove(npu, vlan_rif):
    # Plan: create VLAN 40 and a VLAN RIF on it, read TYPE and VLAN_ID; the
    # fixture removes the RIF and then the VLAN. Verified on saivs, saivpp and
    # saivs-thrift on 21 Oct 2026: a member-less VLAN RIF is accepted on all three.
    rif_type = npu.get(vlan_rif.rif, ["SAI_ROUTER_INTERFACE_ATTR_TYPE"]).value()
    assert rif_type == "SAI_ROUTER_INTERFACE_TYPE_VLAN"
    assert npu.get(vlan_rif.rif, ["SAI_ROUTER_INTERFACE_ATTR_VLAN_ID"]).oid() == vlan_rif.vlan


def test_poc_l3_ipv6_neighbor_create_remove(npu, port_rif, neighbor_nexthop):
    # Plan: IPv6 neighbor on a port RIF, read DST_MAC_ADDRESS, remove it.
    # The next hop is created too, as in the IPv4 test. Same MAC placeholder.
    neighbor_key, nexthop_oid = neighbor_nexthop.create(port_rif, NEIGHBOR_IP_V6, NEIGHBOR_MAC)
    mac = npu.get(neighbor_key, ["SAI_NEIGHBOR_ENTRY_ATTR_DST_MAC_ADDRESS", "00:00:00:00:00:00"]).value()
    assert mac.lower() == NEIGHBOR_MAC
    neighbor_nexthop.remove(nexthop_oid)
    neighbor_nexthop.remove(neighbor_key)


@pytest.mark.negative
def test_poc_l3_route_duplicate_rejected(npu, vrf):
    # "Adding the same route twice fails" (plan). All three targets return
    # SAI_STATUS_ITEM_ALREADY_EXISTS (verified on saivs, saivpp and saivs-thrift,
    # 21 Oct 2026), so the exact status is asserted. A target that deviates is
    # recorded in its profile under expected_gaps.
    cpu_port_oid = _cpu_port_oid(npu)
    npu.create_route(ROUTE_V4, vrf, cpu_port_oid, [PACKET_ACTION, FORWARD])
    try:
        route_key = npu._route_entry_key(vrf, ROUTE_V4)
        # npu.create_route has no do_assert option, so call npu.create directly.
        # Fresh attribute list: the client rewrites list items in place.
        result = npu.create(route_key, [NEXT_HOP_ID, cpu_port_oid, PACKET_ACTION, FORWARD], False)
        assert _status(result) == "SAI_STATUS_ITEM_ALREADY_EXISTS"
    finally:
        npu.remove_route(ROUTE_V4, vrf)


@pytest.mark.negative
def test_poc_l3_route_invalid_nexthop_rejected(npu, vrf):
    # The VRF OID is a well-formed, existing OID of a type that
    # SAI_ROUTE_ENTRY_ATTR_NEXT_HOP_ID does not accept (sairoute.h lists NEXT_HOP,
    # NEXT_HOP_GROUP, ROUTER_INTERFACE and PORT). All three targets return
    # SAI_STATUS_INVALID_PARAMETER (verified on saivs, saivpp and saivs-thrift,
    # 21 Oct 2026), so the exact status is asserted.
    # Note: a RIF OID is a valid next hop id for a route; do not use one here.
    route_key = npu._route_entry_key(vrf, ROUTE_V4)
    status = _status(npu.create(route_key, [NEXT_HOP_ID, vrf], False))
    try:
        assert status == "SAI_STATUS_INVALID_PARAMETER"
    finally:
        if status == SUCCESS:  # nothing must be left behind
            npu.remove(route_key)


@pytest.mark.negative
def test_poc_l3_nexthop_in_use_rejected(npu, vrf, port_rif, neighbor_nexthop):
    _, nexthop_oid = neighbor_nexthop.create(port_rif, NEIGHBOR_IP, NEIGHBOR_MAC)
    npu.create_route(ROUTE_V4, vrf, nexthop_oid)
    try:
        status = neighbor_nexthop.remove(nexthop_oid, do_assert=False)
        assert status == "SAI_STATUS_OBJECT_IN_USE"
    finally:
        # The route must go before the fixture removes the next hop
        npu.remove_route(ROUTE_V4, vrf)


def test_poc_l3_route_ipv4_default_create_remove(npu):
    # Edge-case: 0.0.0.0/0 default route (plan, Week 3, Wed 21 Oct).
    # Placed LAST in the file: on saivpp, syncd stops responding after the
    # remove of this route, which would corrupt all subsequent tests. Putting
    # it last means no other test is affected. Recorded as a gap in saivpp.json.
    # On saivs and saivs-thrift this passes normally.
    #
    # This test does NOT use the vrf fixture: if remove_route hangs syncd, the
    # fixture teardown would also time out and further corrupt the session.
    # Both removals use do_assert=False so the test completes cleanly on all
    # targets regardless of whether syncd responds.
    vrf_oid = npu.create(SaiObjType.VIRTUAL_ROUTER, [])
    route_created = False
    try:
        npu.create_route(ROUTE_V4_DEFAULT, vrf_oid, _cpu_port_oid(npu), [PACKET_ACTION, FORWARD])
        route_created = True
        route_key = npu._route_entry_key(vrf_oid, ROUTE_V4_DEFAULT)
        assert npu.get(route_key, [PACKET_ACTION, ""]).value() == FORWARD
        assert npu.get(route_key, [NEXT_HOP_ID, "oid:0x0"]).oid() == _cpu_port_oid(npu)
    finally:
        if route_created:
            npu.remove(npu._route_entry_key(vrf_oid, ROUTE_V4_DEFAULT), do_assert=False)
        npu.remove(vrf_oid, do_assert=False)
