"""Unit checks for l2.is_mld: pure Python, no traffic needed."""
import pytest
from scapy.all import (
    IP,
    TCP,
    UDP,
    Dot1Q,
    Ether,
    ICMPv6MLDMultAddrRec,
    ICMPv6MLReport2,
    ICMPv6ND_RS,
    IPv6,
    IPv6ExtHdrHopByHop,
    RouterAlert,
)

from .lib import l2

pytestmark = pytest.mark.l2

MLD2 = (Ether(dst="33:33:00:00:00:16")
        / IPv6(dst="ff02::16", hlim=1)
        / IPv6ExtHdrHopByHop(options=[RouterAlert()])
        / ICMPv6MLReport2(records=[ICMPv6MLDMultAddrRec(dst="ff02::1")]))


@pytest.mark.parametrize("frame", [
    MLD2,
    Ether() / Dot1Q(vlan=20) / MLD2.payload,
])
def test_poc_mld_is_detected(frame):
    assert l2.is_mld(bytes(frame))
    assert not l2.not_mld_filter(bytes(frame))


@pytest.mark.parametrize("frame", [
    Ether() / IP() / TCP(),
    Ether() / IP() / UDP(),
    Ether() / IPv6() / ICMPv6ND_RS(),
    Ether() / Dot1Q(vlan=20) / IP() / TCP(),
])
def test_poc_mld_other_frames_kept(frame):
    assert not l2.is_mld(bytes(frame))
    assert l2.not_mld_filter(bytes(frame))
