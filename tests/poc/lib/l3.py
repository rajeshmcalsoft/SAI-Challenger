"""
Track B (L3 unicast routing) fixtures.

Each fixture creates what a test needs, hands it over, and undoes it in
reverse order afterwards, even if the test fails. The SAI calls are copied
from upstream: tests/ut/test_vrf_ut.py (VRF), tests/test_vrf.py::test_rif_create_remove
(port RIF) and tests/native/test_route.py::test_route_update (neighbor, next hop).
"""

from types import SimpleNamespace

import pytest
from saichallenger.common.sai_data import SaiObjType


def _remove_all(npu, handles):
    """Remove handles in reverse order; try all, then raise the first failure."""
    first_error = None
    for handle in reversed(handles):
        try:
            npu.remove(handle)
        except Exception as err:  # noqa: BLE001
            if first_error is None:
                first_error = err
    if first_error is not None:
        raise first_error


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
def port_rif(npu, vrf, poc_ports):
    """
    Turn the first profile port (poc_ports(1)) into a routed port in the `vrf` VRF
    and yield the RIF OID. Afterwards the port is put back into the default VLAN.
    """
    # Assumes poc_ports(n) returns a list of port indices usable in npu.port_oids
    # and npu.dot1q_bp_oids (per the Week 2-3 plan); confirm in tests/poc/conftest.py.
    port_idx = poc_ports(1)[0]
    port_oid = npu.port_oids[port_idx]
    bp_oid = npu.dot1q_bp_oids[port_idx]

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
            npu.dot1q_bp_oids[port_idx] = new_bp_oid
            bp_oid = new_bp_oid

        if vlan_member_removed:
            npu.create_vlan_member(
                npu.default_vlan_oid, bp_oid, "SAI_VLAN_TAGGING_MODE_UNTAGGED"
            )
            npu.set(port_oid, ["SAI_PORT_ATTR_PORT_VLAN_ID", npu.default_vlan_id])


@pytest.fixture
def neighbor_nexthop(npu):
    """
    Helper object for neighbors and next hops on a RIF:

      neighbor_nexthop.create(rif_oid, ip, mac) -> (neighbor_key, nexthop_oid)
          Creates a neighbor entry, then a next hop for `ip` on the RIF.
          Can be called more than once per test (e.g. two next hops on one RIF).
      neighbor_nexthop.remove(handle, do_assert=True) -> status
          Removes a neighbor key or next hop OID that was created here. The
          handle is forgotten only if the removal succeeded, so a rejected
          removal (do_assert=False, e.g. SAI_STATUS_OBJECT_IN_USE) is still
          cleaned up at teardown.

    Order follows upstream test_route_update: neighbor first, then next hop.
    Teardown removes whatever is left in reverse order of creation, even if the
    test fails part way.

    Objects that depend on these (routes, next hop group members) must be removed
    by the test, or by a fixture that depends on this one, before teardown runs;
    otherwise the next hop removal is rejected as in use. The RIF fixture must be
    set up before this one (list it first in the test arguments) so that it is
    torn down after this one.
    """
    created = []  # handles in creation order: neighbor keys and next hop OIDs

    def create(rif_oid, ip, mac):
        neighbor_key = npu._neighbor_entry_key(rif_oid, ip)
        npu.create(neighbor_key, ["SAI_NEIGHBOR_ENTRY_ATTR_DST_MAC_ADDRESS", mac])
        created.append(neighbor_key)

        nexthop_oid = npu.create(
            SaiObjType.NEXT_HOP,
            [
                "SAI_NEXT_HOP_ATTR_TYPE", "SAI_NEXT_HOP_TYPE_IP",
                "SAI_NEXT_HOP_ATTR_IP", ip,
                "SAI_NEXT_HOP_ATTR_ROUTER_INTERFACE_ID", rif_oid,
            ],
        )
        created.append(nexthop_oid)
        return neighbor_key, nexthop_oid

    def remove(handle, do_assert=True):
        status = npu.remove(handle, do_assert)
        if status == "SAI_STATUS_SUCCESS":
            created.remove(handle)
        return status

    try:
        yield SimpleNamespace(create=create, remove=remove)
    finally:
        _remove_all(npu, created)


@pytest.fixture
def next_hop_group(npu, neighbor_nexthop):
    """
    Helper object for next hop groups:

      next_hop_group.create() -> group_oid
      next_hop_group.add_member(group_oid, nexthop_oid) -> member_oid
      next_hop_group.remove(handle, do_assert=True) -> status

    Teardown removes whatever is left in reverse order (members before their
    group). It depends on `neighbor_nexthop` only so that pytest tears this
    fixture down first: members reference next hops, so they must go first.

    The group type is the one used by upstream tests/api/test_next_hop_group.py.
    """
    created = []  # group and member OIDs in creation order

    def create():
        group_oid = npu.create(
            SaiObjType.NEXT_HOP_GROUP,
            ["SAI_NEXT_HOP_GROUP_ATTR_TYPE", "SAI_NEXT_HOP_GROUP_TYPE_DYNAMIC_UNORDERED_ECMP"],
        )
        created.append(group_oid)
        return group_oid

    def add_member(group_oid, nexthop_oid):
        member_oid = npu.create(
            SaiObjType.NEXT_HOP_GROUP_MEMBER,
            [
                "SAI_NEXT_HOP_GROUP_MEMBER_ATTR_NEXT_HOP_GROUP_ID", group_oid,
                "SAI_NEXT_HOP_GROUP_MEMBER_ATTR_NEXT_HOP_ID", nexthop_oid,
            ],
        )
        created.append(member_oid)
        return member_oid

    def remove(handle, do_assert=True):
        status = npu.remove(handle, do_assert)
        if status == "SAI_STATUS_SUCCESS":
            created.remove(handle)
        return status

    try:
        yield SimpleNamespace(create=create, add_member=add_member, remove=remove)
    finally:
        _remove_all(npu, created)
