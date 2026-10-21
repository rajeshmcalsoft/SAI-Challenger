"""Track C port counter test (Level 2): the ingress packet counter goes up by 10.

test_poc_counters_delta sends PACKETS frames into port IN_IDX and checks that the
port's ingress packet counter rose by that many:

    PACKETS <= counter after - counter before <= PACKETS + CONTROL_ALLOWANCE

CONTROL_ALLOWANCE covers frames the switch sends or receives by itself (neighbour
discovery, MLD and similar control traffic).

The test never clears the counters. test_poc_counters_clear_read showed a counter
is not reliably zero after a clear (control traffic arrives in between), so only a
before/after difference is used.

Packets really have to go through the switch, so the test first puts ports IN_IDX
and OUT_IDX in VLAN 10 with static FDB entries (the setup of the ACL traffic
tests), and checks that the first frame comes out of OUT_IDX. If that fails, the
data plane is the problem, not the counter. The VLAN setup is removed again at
the end, even when the test fails.

Diagnostics: besides the checked counter the test reads a list of related port
counters (CANDIDATE_STATS) on the in and out ports before and after, and prints
every one that moved (RESULT counters_moved ...). A counter the target does not
support is printed as n/a and never fails the test. If the checked counter does
not move but another one does, change IN_PKTS_STAT to that one.

Two tests share the same measurement:
  test_poc_counters_delta         ingress counter of the port the frames enter (the
                                  plan's test)
  test_poc_counters_egress_delta  egress counter of the port the frames leave
On 21 Oct saivpp showed ingress counters that never move (all IF_IN_* stayed 0 for
forwarded frames) while the egress counters counted exactly the frames sent, so the
ingress test is a gap on saivpp (declared in targets/saivpp.json) and the egress
test passes.

It needs packets, so it runs only when npu.run_traffic is True (saivpp with
--traffic) and SKIPS everywhere else.
"""
import time
from types import SimpleNamespace

import pytest
from saichallenger.common.sai_data import SaiObjType

try:
    from ptf.testutils import send_packet, simple_tcp_packet, verify_packet
    PTF_IMPORT_ERROR = None
except ImportError as exc:                    # only needed when packets are sent
    PTF_IMPORT_ERROR = exc

pytestmark = [pytest.mark.counters]

IN_IDX, OUT_IDX = 0, 1           # frames go in on IN_IDX, out of OUT_IDX
PACKETS = 10                     # frames sent
CONTROL_ALLOWANCE = 5            # extra frames allowed (control traffic)
SETTLE_SECONDS = 10              # how long to wait for the counter to catch up
POLL_SECONDS = 1

IN_PKTS_STAT = "SAI_PORT_STAT_IF_IN_UCAST_PKTS"      # checked by the ingress test
OUT_PKTS_STAT = "SAI_PORT_STAT_IF_OUT_UCAST_PKTS"    # checked by the egress test
CANDIDATE_STATS = [                                  # printed for diagnosis only
    "SAI_PORT_STAT_IF_IN_UCAST_PKTS",
    "SAI_PORT_STAT_IF_IN_NON_UCAST_PKTS",
    "SAI_PORT_STAT_IF_IN_OCTETS",
    "SAI_PORT_STAT_IF_IN_DISCARDS",
    "SAI_PORT_STAT_IF_IN_ERRORS",
    "SAI_PORT_STAT_ETHER_IN_PKTS_65_TO_127_OCTETS",
    "SAI_PORT_STAT_IF_OUT_UCAST_PKTS",
    "SAI_PORT_STAT_IF_OUT_NON_UCAST_PKTS",
    "SAI_PORT_STAT_IF_OUT_OCTETS",
    "SAI_PORT_STAT_ETHER_OUT_PKTS_65_TO_127_OCTETS",
]

L2_VLAN_ID = "10"
SRC_MAC = "00:11:11:11:11:11"
DST_MAC = "00:22:22:22:22:22"
UNTAGGED = "SAI_VLAN_TAGGING_MODE_UNTAGGED"
PVID_ATTR = "SAI_PORT_ATTR_PORT_VLAN_ID"


def _attempt(problems, label, fn):
    try:
        fn()
    except Exception as exc:                  # noqa: BLE001
        problems.append(f"{label}: {str(exc)[:150]}")


def l2_teardown(npu, s):
    """Undo the L2 setup in reverse order. Safe to call twice; returns problems."""
    problems = []
    for mac in reversed(s.fdb):
        _attempt(problems, f"remove fdb {mac}", lambda m=mac: npu.remove_fdb(s.vlan, m))
    s.fdb = []
    for oid in reversed(s.vlan_members):
        _attempt(problems, "remove vlan member", lambda o=oid: npu.remove(o))
    s.vlan_members = []
    for bp in s.default_removed:
        _attempt(problems, "restore default vlan member",
                 lambda b=bp: npu.create_vlan_member(npu.default_vlan_oid, b, UNTAGGED))
    s.default_removed = []
    for port in s.pvid_changed:
        _attempt(problems, "restore pvid",
                 lambda p=port: npu.set(p, [PVID_ATTR, npu.default_vlan_id]))
    s.pvid_changed = []
    if s.vlan:
        _attempt(problems, "remove vlan", lambda: npu.remove(s.vlan))
        s.vlan = None
    return problems


def l2_setup(npu, s):
    """VLAN 10 with IN_IDX and OUT_IDX as untagged members and one static FDB entry
    per MAC. Everything is recorded in `s` so l2_teardown can undo it."""
    s.vlan = npu.create(SaiObjType.VLAN, ["SAI_VLAN_ATTR_VLAN_ID", L2_VLAN_ID])
    for idx in (IN_IDX, OUT_IDX):
        bp, port = npu.dot1q_bp_oids[idx], npu.port_oids[idx]
        npu.remove_vlan_member(npu.default_vlan_oid, bp)
        s.default_removed.append(bp)
        s.vlan_members.append(npu.create_vlan_member(s.vlan, bp, UNTAGGED))
        npu.set(port, [PVID_ATTR, L2_VLAN_ID])
        s.pvid_changed.append(port)
    for mac, idx in ((SRC_MAC, IN_IDX), (DST_MAC, OUT_IDX)):
        npu.create_fdb(s.vlan, mac, npu.dot1q_bp_oids[idx])
        s.fdb.append(mac)


def read_stat(npu, port_oid, stat):
    """One counter as an int, or None when the target cannot read it."""
    try:
        return int(npu.get_stats(port_oid, [stat, ""]).counters()[stat])
    except Exception:                         # noqa: BLE001
        return None


def read_all(npu, port_oids):
    return {name: {stat: read_stat(npu, oid, stat) for stat in CANDIDATE_STATS}
            for name, oid in port_oids.items()}


def moved(before, after):
    """{port: {stat: delta}} for every counter that changed or cannot be read."""
    result = {}
    for port, stats in after.items():
        for stat, value in stats.items():
            old = before[port][stat]
            if value is None or old is None:
                result.setdefault(port, {})[stat.replace("SAI_PORT_STAT_", "")] = "n/a"
            elif value != old:
                result.setdefault(port, {})[stat.replace("SAI_PORT_STAT_", "")] = value - old
    return result


@pytest.fixture
def l2(npu):
    """VLAN 10 with ports IN_IDX and OUT_IDX, removed again after the test."""
    state = SimpleNamespace(vlan=None, vlan_members=[], default_removed=[],
                            fdb=[], pvid_changed=[])
    try:
        l2_setup(npu, state)
        yield state
    finally:
        problems = l2_teardown(npu, state)
    assert not problems, f"L2 cleanup left problems: {problems}"

def measure(npu, dataplane, which, stat):
    ports = {
        "in": npu.port_oids[IN_IDX],
        "out": npu.port_oids[OUT_IDX],
    }

    print("RESULT ingress_port_oid =", npu.port_oids[IN_IDX])
    print("RESULT egress_port_oid =", npu.port_oids[OUT_IDX])
    print("RESULT ingress_stat =", IN_PKTS_STAT)
    pkt = simple_tcp_packet(eth_src=SRC_MAC, eth_dst=DST_MAC,
                            ip_src="20.0.0.2", ip_dst="192.168.1.10",
                            tcp_sport=1234, tcp_dport=80, ip_ttl=64)

    # The data plane must work before a counter result means anything.
    send_packet(dataplane, IN_IDX, pkt)
    try:
        verify_packet(dataplane, pkt, OUT_IDX, timeout=2)
    except AssertionError as exc:
        pytest.fail(f"control check failed: the frame did not come out of port "
                    f"{OUT_IDX}, so the data plane is not forwarding: {exc}")

    all_before = read_all(npu, ports)
    before = read_stat(npu, ports[which], stat)
    assert before is not None, f"{stat} cannot be read on this target"

    for _ in range(PACKETS):
        send_packet(dataplane, IN_IDX, pkt)

    # The counter is refreshed from the data plane with a delay: poll until it
    # has caught up, and keep the last value if it never does.
    after = before
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        after = read_stat(npu, ports[which], stat)
        if after - before >= PACKETS or time.monotonic() >= deadline:
            break
        time.sleep(POLL_SECONDS)

    changed = moved(all_before, read_all(npu, ports))
    print(f"RESULT counters_delta sent={PACKETS} {stat}: {before} -> {after} "
          f"(delta {after - before})")
    print(f"RESULT counters_moved {changed}")
    return before, after


def check_delta(before, after, stat):
    delta = after - before
    assert delta >= PACKETS, (
        f"{stat} rose by {delta} after {PACKETS} frames were sent "
        f"(waited {SETTLE_SECONDS}s): frames are not counted. "
        "See the 'RESULT counters_moved' line for counters that did move.")
    assert delta <= PACKETS + CONTROL_ALLOWANCE, (
        f"{stat} rose by {delta}, more than {PACKETS} sent + "
        f"{CONTROL_ALLOWANCE} allowed control frames")


def need_traffic(npu):
    if not npu.run_traffic:
        pytest.skip("traffic mode is off (npu.run_traffic is False): no dataplane ports")
    assert PTF_IMPORT_ERROR is None, f"ptf.testutils not importable: {PTF_IMPORT_ERROR}"

@pytest.mark.traffic
def test_poc_counters_delta(npu, dataplane, l2):
    """Ingress: 10 frames should increase the ingress counter by 10."""
    need_traffic(npu)
    before, after = measure(npu, dataplane, "in", IN_PKTS_STAT)
    check_delta(before, after, IN_PKTS_STAT)


@pytest.mark.traffic
def test_poc_counters_egress_delta(npu, dataplane, l2):
    """Egress: 10 frames should increase the egress counter by 10."""
    need_traffic(npu)
    before, after = measure(npu, dataplane, "out", OUT_PKTS_STAT)
    check_delta(before, after, OUT_PKTS_STAT)
