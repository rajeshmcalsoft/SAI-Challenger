"""Shared pytest setup for tests/poc (mounted in the containers as tests/poc_<name>).

All target-specific information comes from tests/poc/targets/<profile>.json.
run_poc.sh selects the profile (env POC_TARGET_PROFILE, or --target-profile).
"""
import fnmatch
import json
import os
import re
import time
from pathlib import Path

import pytest

from .lib import l2

HERE = Path(__file__).resolve().parent
TARGETS = HERE / "targets"
REPORTS = HERE / "reports"
DEFAULT_PORTS = list(range(8))
PCAP_IFACES = ["veth1", "veth2", "veth3"]  # PTF ports 0-2 (see the testbed file)
PROFILE = pytest.StashKey[dict]()

MARKERS = (
    ("l2", "Track A: L2 forwarding and VLANs"),
    ("l3", "Track B: L3 unicast routing"),
    ("acl", "Track C: ACL"),
    ("counters", "Track C: port counters"),
    ("traffic", "needs --traffic and a forwarding target"),
    ("negative", "bad input must be rejected"),
)


def pytest_addoption(parser):
    parser.addoption(
        "--target-profile",
        default=os.environ.get("POC_TARGET_PROFILE", ""),
        help="profile name in tests/poc/targets/ (run_poc.sh sets it)",
    )
    parser.addoption(
        "--mld-filter",
        choices=("auto", "on", "off"),
        default="auto",
        help="auto: follow the profile's traffic_filters; on/off: override (verification runs)",
    )


def pytest_configure(config):
    for name, text in MARKERS:
        config.addinivalue_line("markers", f"{name}: {text}")
    name = config.getoption("--target-profile")
    profile = {}
    if name:
        path = TARGETS / f"{name}.json"
        if not path.is_file():
            raise pytest.UsageError(f"target profile not found: {path}")
        profile = json.loads(path.read_text())
    config.stash[PROFILE] = profile


def _mld_wanted(config):
    mode = config.getoption("--mld-filter")
    if mode != "auto":
        return mode == "on"
    return "mld" in config.stash[PROFILE].get("traffic_filters", [])


def pytest_report_header(config):
    name = config.getoption("--target-profile") or "(none)"
    return f"poc target profile: {name} | mld filter: {'on' if _mld_wanted(config) else 'off'}"


def pytest_collection_modifyitems(config, items):
    """Mark every test named in the profile's expected_gaps as strict xfail."""
    gaps = config.stash[PROFILE].get("expected_gaps", {})
    for item in items:
        for pattern, reason in gaps.items():
            # '[' is literal (parametrized ids like name[4095]); '*' and '?' stay wildcards
            if fnmatch.fnmatchcase(item.name, pattern.replace("[", "[[]")):
                item.add_marker(pytest.mark.xfail(reason=reason, strict=True))
                break


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


@pytest.fixture(autouse=True)
def traffic_gate(request, npu):
    """Tests marked 'traffic' are skipped unless the run sends traffic on a forwarding target."""
    if request.node.get_closest_marker("traffic") and not npu.run_traffic:
        pytest.skip("needs --traffic on a forwarding target")


@pytest.fixture(scope="session", autouse=True)
def mld_filter(request):
    if not _mld_wanted(request.config):
        yield
        return
    l2.install_mld_filter()
    yield
    l2.remove_mld_filter()


@pytest.fixture
def poc_ports(request, npu):
    """poc_ports(n) -> the first n port indices of the profile; skips if there are fewer."""
    ports = request.config.stash[PROFILE].get("ports", DEFAULT_PORTS)

    def get(n):
        if len(ports) < n:
            pytest.skip(f"profile provides fewer than {n} ports")
        chosen = ports[:n]
        assert max(chosen) < len(npu.port_oids), f"profile port {max(chosen)} not on this switch"
        return chosen

    return get


@pytest.fixture
def pcap_capture(request, npu):
    """Sniff the dataplane veths during a test; save reports/<test>.pcap."""
    if not npu.run_traffic:
        yield None
        return
    # imported here so runs without traffic never need scapy
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
