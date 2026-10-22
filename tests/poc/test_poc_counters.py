"""Track C (port counters) Level 1 PoC test.

test_poc_counters_clear_read: clear the counters of one port, read them back,
expect zero. Creates no SAI objects, so there is nothing to clean up.

Written to the SAI spec; no target checks. If a target does not support a
counter, SAI says so with a status code and the test skips with that reason.
"""
import pytest

pytestmark = [pytest.mark.counters]

STATS = [
    "SAI_PORT_STAT_IF_IN_OCTETS",
    "SAI_PORT_STAT_IF_IN_UCAST_PKTS",
    "SAI_PORT_STAT_IF_OUT_OCTETS",
    "SAI_PORT_STAT_IF_OUT_UCAST_PKTS",
]
RETRIES = 3     # a stray control packet can land between clear and read

@pytest.fixture
def counter_port(npu):
    """Return the first available port OID."""
    if not npu.port_oids:
        pytest.skip("No port OIDs available")

    if isinstance(npu.port_oids, dict):
        return next(iter(npu.port_oids.values()))

    return npu.port_oids[0]

def read_counters(npu, port):
    """Return {stat_name: int} for STATS."""
    data = npu.get_stats(port, STATS)
    values = data.counters() if hasattr(data, "counters") else data
    if isinstance(values, dict):
        # a target may return only the counters it supports
        return {name: int(value) for name, value in values.items()}
    return {name: int(value) for name, value in zip(STATS, values)}


def test_poc_counters_clear_read(npu, counter_port):
    try:
        before = read_counters(npu, counter_port)
    except Exception as exc:
        if "NOT_IMPLEMENTED" in str(exc) or "NOT_SUPPORTED" in str(exc):
            pytest.skip(f"port counters not supported on this target: {exc}")
        raise
    print(f"RESULT counters before clear: {before}")
    if not before:
        pytest.skip("target returned no port counters")

    after = {}
    for attempt in range(1, RETRIES + 1):
        npu.clear_stats(counter_port, STATS)
        after = read_counters(npu, counter_port)
        print(f"RESULT counters after clear (try {attempt}): {after}")
        if not any(after.values()):
            break

    assert not any(after.values()), f"counters not zero after clear: {after}"
