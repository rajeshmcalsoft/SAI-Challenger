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
    """Save frames seen on veth1/veth2 to reports/<test>.pcap (saivpp with --traffic only)."""
    if not npu.run_traffic:
        yield None
        return
    from scapy.all import AsyncSniffer, wrpcap  # only the saivpp container needs this
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / (re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name) + ".pcap")
    sniffer = AsyncSniffer(iface=PCAP_IFACES)
    sniffer.start()
    time.sleep(1)  # let the sniffer attach before the test sends
    try:
        yield path
    finally:
        if sniffer.running:
            sniffer.stop()
        wrpcap(str(path), sniffer.results or [])
