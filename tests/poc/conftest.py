"""Shared pytest setup for tests/poc (mounted in the containers as tests/poc_<name>)."""
import re
import time
from pathlib import Path

import pytest

from .lib import l2

MARKERS = (
    ("l2", "Track A: L2 forwarding and VLANs"),
    ("l3", "Track B: L3 unicast routing"),
    ("acl", "Track C: ACL"),
    ("counters", "Track C: port counters"),
    ("traffic", "needs --traffic and a forwarding target (saivpp)"),
    ("negative", "bad input must be rejected"),
)

# PTF port 0 and 1 map to veth1 and veth2 (testbeds/saivpp_standalone.json)
PCAP_IFACES = ["veth1", "veth2"]
REPORTS = Path(__file__).resolve().parent / "reports"

def pytest_addoption(parser):
    parser.addoption(
        "--mld-filter",
        choices=("on", "off"),
        default="off",
        help="on: PTF ignores stray MLD frames; off: they show up as unexpected packets",
    )

def pytest_configure(config):
    for name, text in MARKERS:
        config.addinivalue_line("markers", f"{name}: {text}")

@pytest.fixture(scope="module", autouse=True)
def skip_all(testbed_instance):
    testbed = testbed_instance
    if testbed is not None and len(testbed.npu) != 1:
        pytest.skip(f'invalid for "{testbed.name}" testbed')


@pytest.fixture(autouse=True)
def on_prev_test_failure(prev_test_failed, npu):
    """Reset the switch after a failed test (same as upstream test_l2_basic.py)."""
    if prev_test_failed:
        npu.reset()

@pytest.fixture(scope="session", autouse=True)
def mld_filter(request):
    if request.config.getoption("--mld-filter") != "on":
        yield
        return
    l2.install_mld_filter()
    yield
    l2.remove_mld_filter()

@pytest.fixture
def pcap_capture(request, npu):
    """Sniff the dataplane veths during a test; save reports/<test>.pcap."""
    if not npu.run_traffic:
        yield None
        return
    # imported here so saivs runs never need scapy
    from scapy.all import AsyncSniffer, wrpcap

    REPORTS.mkdir(exist_ok=True)
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
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)
        path = REPORTS / f"{name}.pcap"
        wrpcap(str(path), pkts)
        print(f"pcap written: {path} ({len(pkts)} packets)")
