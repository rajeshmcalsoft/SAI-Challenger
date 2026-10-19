"""Track A, Level 2 (data plane): L2 forwarding. Needs --traffic on a forwarding target.

Without --traffic (or on a target that does not forward) these tests are skipped.
verify_packets also fails on any unexpected frame on any other port.
"""
import time

import pytest
from ptf.testutils import (
    send_packet,
    simple_tcp_packet,
    verify_no_packet,
    verify_packets,
)

from .lib import l2

pytestmark = [pytest.mark.l2, pytest.mark.traffic]

VLAN_ID = "20"
MACS = ["00:11:11:11:11:11", "00:22:22:22:22:22", "00:33:33:33:33:33"]
BCAST = "ff:ff:ff:ff:ff:ff"


def tcp(eth_dst, eth_src, vlan=None, ip_id=101):
    """A TCP packet; with vlan set it carries that 802.1Q tag (104 bytes, as upstream)."""
    args = {
        "eth_dst": eth_dst,
        "eth_src": eth_src,
        "ip_dst": "10.0.0.1",
        "ip_id": ip_id,
        "ip_ttl": 64,
    }
    if vlan is not None:
        args.update(dl_vlan_enable=True, vlan_vid=int(vlan), pktlen=104)
    return simple_tcp_packet(**args)


def test_poc_l2_access_forward_both_ways(npu, dataplane, pcap_capture, poc_ports):
    """VLAN 20 on three access ports: frames p0 -> p2 and p2 -> p0 arrive only on the destination."""
    p0, p1, p2 = poc_ports(3)
    with l2.access_vlan(npu, VLAN_ID, {p0: MACS[0], p1: MACS[1], p2: MACS[2]}):
        forward = tcp(MACS[2], MACS[0], ip_id=101)
        send_packet(dataplane, p0, forward)
        verify_packets(dataplane, forward, [p2])
        back = tcp(MACS[0], MACS[2], ip_id=102)
        send_packet(dataplane, p2, back)
        verify_packets(dataplane, back, [p0])


# ---- end of Tuesday ----


def test_poc_l2_access_to_trunk_tag_push(npu, dataplane, pcap_capture, poc_ports):
    """An untagged frame from an access port leaves the trunk port tagged with VLAN 20."""
    p0, p1 = poc_ports(2)
    with l2.vlan(npu, VLAN_ID) as vlan_oid, \
            l2.vlan_member(npu, vlan_oid, p0, l2.UNTAGGED), \
            l2.vlan_member(npu, vlan_oid, p1, l2.TAGGED), \
            l2.static_fdb(npu, vlan_oid, MACS[0], p0), \
            l2.static_fdb(npu, vlan_oid, MACS[1], p1):
        sent = tcp(MACS[1], MACS[0])
        expected = tcp(MACS[1], MACS[0], vlan=VLAN_ID)
        send_packet(dataplane, p0, sent)
        verify_packets(dataplane, expected, [p1])


def test_poc_l2_trunk_to_access_tag_pop(npu, dataplane, pcap_capture, poc_ports):
    """A tagged frame from the trunk port leaves the access port untagged."""
    p0, p1 = poc_ports(2)
    with l2.vlan(npu, VLAN_ID) as vlan_oid, \
            l2.vlan_member(npu, vlan_oid, p0, l2.TAGGED), \
            l2.vlan_member(npu, vlan_oid, p1, l2.UNTAGGED), \
            l2.static_fdb(npu, vlan_oid, MACS[0], p0), \
            l2.static_fdb(npu, vlan_oid, MACS[1], p1):
        sent = tcp(MACS[1], MACS[0], vlan=VLAN_ID)
        expected = tcp(MACS[1], MACS[0])
        send_packet(dataplane, p0, sent)
        verify_packets(dataplane, expected, [p1])


def test_poc_l2_unknown_unicast_flood(npu, dataplane, pcap_capture, poc_ports):
    """A frame to an unknown MAC reaches every other VLAN member and no other port."""
    p0, p1, p2 = poc_ports(3)
    with l2.vlan(npu, VLAN_ID) as vlan_oid, \
            l2.vlan_member(npu, vlan_oid, p0), \
            l2.vlan_member(npu, vlan_oid, p1), \
            l2.vlan_member(npu, vlan_oid, p2), \
            l2.flush_on_exit(npu, vlan_oid):
        flooded = tcp(MACS[2], MACS[0])  # no FDB entry for MACS[2]
        send_packet(dataplane, p0, flooded)
        verify_packets(dataplane, flooded, [p1, p2])


# ---- end of Wednesday ----


def test_poc_l2_broadcast_flood(npu, dataplane, pcap_capture, poc_ports):
    """A broadcast frame reaches every other VLAN member and no other port."""
    p0, p1, p2 = poc_ports(3)
    with l2.vlan(npu, VLAN_ID) as vlan_oid, \
            l2.vlan_member(npu, vlan_oid, p0), \
            l2.vlan_member(npu, vlan_oid, p1), \
            l2.vlan_member(npu, vlan_oid, p2), \
            l2.flush_on_exit(npu, vlan_oid):
        bcast = tcp(BCAST, MACS[0])
        send_packet(dataplane, p0, bcast)
        verify_packets(dataplane, bcast, [p1, p2])


def test_poc_l2_mac_learning(npu, dataplane, pcap_capture, poc_ports):
    """After one frame from MAC A, a frame to MAC A goes only to A's port (model: test_l2_mac_move_1)."""
    p0, p1, p2 = poc_ports(3)
    with l2.vlan(npu, VLAN_ID) as vlan_oid, \
            l2.vlan_member(npu, vlan_oid, p0), \
            l2.vlan_member(npu, vlan_oid, p1), \
            l2.vlan_member(npu, vlan_oid, p2), \
            l2.flush_on_exit(npu, vlan_oid):
        a_to_b = tcp(MACS[1], MACS[0], ip_id=105)
        b_to_a = tcp(MACS[0], MACS[1], ip_id=106)
        send_packet(dataplane, p0, a_to_b)               # B unknown: flooded, A learned on p0
        verify_packets(dataplane, a_to_b, [p1, p2])
        send_packet(dataplane, p1, b_to_a)               # A known: only p0; B learned on p1
        verify_packets(dataplane, b_to_a, [p0])
        time.sleep(1)
        send_packet(dataplane, p0, a_to_b)               # B known now: only p1, nothing on p2
        verify_packets(dataplane, a_to_b, [p1])
        verify_no_packet(dataplane, a_to_b, p2)
