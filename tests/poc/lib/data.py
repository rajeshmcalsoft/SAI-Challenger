"""Test data for tests/poc: values a test chooses (VLAN IDs, MACs, addresses).

Each track has its own file, tests/poc/data/<track>.json. Values the SAI spec
defines stay in the test code; target deviations stay in targets/<profile>.json.
Load at import time (D = data.load("l2")) so values can also feed parametrize.
"""
import json
from pathlib import Path
from types import SimpleNamespace

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def load(track):
    """Return tests/poc/data/<track>.json as a namespace (nested dicts too)."""
    path = DATA_DIR / f"{track}.json"
    try:
        return json.loads(path.read_text(), object_hook=lambda d: SimpleNamespace(**d))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot load test data {path}: {exc}") from exc
