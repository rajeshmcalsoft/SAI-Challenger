"""Track C port counters, Level 1: clear a port's counters; they must read 0.

Creates no SAI objects. Skips if the target says counters are unsupported.
"""

import os
import sys

import pytest

# lib/ sits next to this file; make it importable when the file is run alone.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.params import load

pytestmark = [pytest.mark.counters]

# Values come from data/ACL.json ("counters" -> "clear_read").
_C = load()["counters"]["clear_read"]
STATS = _C["stats"]
RETRIES = _C["retries"]  # Allow for stray control packets between clear and read.


@pytest.fixture
def counter_port(npu, poc_ports):
    """First port of the target profile, returned as a port OID."""
    idx = poc_ports(1)[0]  # poc_ports is a factory: call it, then index
    return npu.port_oids[idx]


def read_counters(npu, port):
    """Return supported counters as a {stat_name: integer_value} dictionary."""
    data = npu.get_stats(port, STATS)
    values = data.counters() if hasattr(data, "counters") else data

    if isinstance(values, dict):
        return {name: int(value) for name, value in values.items()}

    return {name: int(value) for name, value in zip(STATS, values)}


def is_unsupported(exc):
    return "NOT_IMPLEMENTED" in str(exc) or "NOT_SUPPORTED" in str(exc)


# Clear a port's counters; they must read 0 afterwards.
def test_poc_counters_clear_read(npu, counter_port):
    """Verify that clearing supported port counters resets them to zero."""
    try:
        before = read_counters(npu, counter_port)
    except Exception as exc:
        if is_unsupported(exc):
            pytest.skip(f"Port counters are not supported on this target: {exc}")
        raise

    print(f"RESULT counters before clear: {before}")

    if not before:
        pytest.skip("Target returned no port counters")

    after = {}

    for attempt in range(1, RETRIES + 1):
        try:
            npu.clear_stats(counter_port, STATS)
            after = read_counters(npu, counter_port)
        except Exception as exc:
            if is_unsupported(exc):
                pytest.skip(f"Port counter operation is unsupported: {exc}")
            raise

        print(f"RESULT counters after clear (try {attempt}): {after}")

        if not any(after.values()):
            break

    assert not any(after.values()), f"Port counters were not zero after clear: {after}"
