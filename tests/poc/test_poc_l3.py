"""Track B (L3 unicast routing): Level 1 (control plane) tests for VRF and port RIF."""

import pytest

from .lib.l3 import PORT_IDX, vrf, port_rif  # noqa: F401  (vrf, port_rif are pytest fixtures)

pytestmark = pytest.mark.l3


def test_poc_l3_vrf_default_admin_state(request, npu, vrf):
    # A new (non-default) VRF has admin state "true" by default
    # (see TestNonDefaultVrf in tests/ut/test_vrf_ut.py).
    # The attribute is read without setting it first, which saivs does not
    # implement (SAI_STATUS_NOT_IMPLEMENTED), so expect a failure on saivs only.
    if npu.libsaivs:
        request.applymarker(pytest.mark.xfail(
            reason="saivs: get of never-set attribute is NOT_IMPLEMENTED"))
    state = npu.get(vrf, ["SAI_VIRTUAL_ROUTER_ATTR_ADMIN_V4_STATE"]).value()
    assert state == "true"


def test_poc_l3_vrf_create_readback(npu, vrf):
    # saivs cannot read attributes that were never set (see the TODO in
    # tests/test_vrf.py::test_default_vrf), so set first, then read back.
    # "false" differs from the default, so the read-back proves the set worked.
    npu.set(vrf, ["SAI_VIRTUAL_ROUTER_ATTR_ADMIN_V4_STATE", "false"])
    state = npu.get(vrf, ["SAI_VIRTUAL_ROUTER_ATTR_ADMIN_V4_STATE", "true"]).to_json()
    assert state[1].lower() == "false"


def test_poc_l3_port_rif_create_remove(npu, port_rif):
    rif_type = npu.get(port_rif, ["SAI_ROUTER_INTERFACE_ATTR_TYPE"]).value()
    assert rif_type == "SAI_ROUTER_INTERFACE_TYPE_PORT"


def test_poc_l3_port_rif_port_restored(npu):
    # Must run right after the RIF test (pytest keeps file order): its fixture
    # is already torn down, so the port should be back in the default VLAN.
    # get_vlan_member() returns None if it is not.
    bp_oid = npu.dot1q_bp_oids[PORT_IDX]
    assert npu.get_vlan_member(npu.default_vlan_oid, bp_oid) is not None
