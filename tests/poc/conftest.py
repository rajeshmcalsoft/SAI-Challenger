import pytest

MARKERS = {
    "l2": "Track A: L2 forwarding and VLANs",
    "l3": "Track B: L3 unicast routing",
    "acl": "Track C: ACL",
    "counters": "Track C: port counters",
    "traffic": "needs --traffic and a forwarding target (saivpp)",
    "negative": "bad input must be rejected",
}


def pytest_configure(config):
    for name, desc in MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {desc}")


@pytest.fixture(autouse=True)
def on_prev_test_failure(prev_test_failed, npu):
    if prev_test_failed:
        npu.reset()
