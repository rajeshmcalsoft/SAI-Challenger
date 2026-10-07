import os
import time

import pytest

MARKERS = {
    "l2": "Track A: L2 forwarding and VLANs",
    "l3": "Track B: L3 unicast routing",
    "acl": "Track C: ACL",
    "counters": "Track C: port counters",
    "traffic": "needs --traffic and a forwarding target (saivpp)",
    "negative": "bad input must be rejected",
}

# PTF port 0 and 1 map to veth1 and veth2 (testbeds/saivpp_standalone.json)
PCAP_IFACES = ["veth1", "veth2"]
REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")


def pytest_configure(config):
    for name, desc in MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {desc}")


@pytest.fixture(autouse=True)
def on_prev_test_failure(prev_test_failed, npu):
    if prev_test_failed:
        npu.reset()


@pytest.fixture
def pcap_capture(request, npu):
    """Sniff the dataplane veths during a test; save reports/<test>.pcap."""
    if not npu.run_traffic:
        yield None
        return
    # imported here so saivs runs never need scapy
    from scapy.all import AsyncSniffer, wrpcap

    os.makedirs(REPORTS_DIR, exist_ok=True)
    sniffer = AsyncSniffer(iface=PCAP_IFACES, store=True)
    sniffer.start()
    time.sleep(1)  # let the sniffer attach before the test sends anything
    try:
        yield sniffer
    finally:
        time.sleep(0.5)  # catch late packets
        if sniffer.running:
            sniffer.stop()
        pkts = sniffer.results or []
        path = os.path.join(REPORTS_DIR, f"{request.node.name}.pcap")
        wrpcap(path, pkts)
        print(f"pcap written: {path} ({len(pkts)} packets)")
