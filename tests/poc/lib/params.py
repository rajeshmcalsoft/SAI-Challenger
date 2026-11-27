"""lib/params.py - loads tests/poc/data/ACL.json (every tunable value of the poc tests lives there).

    from lib.params import load
    P = load()["acl"]

POC_PARAMS=<file> selects another JSON file. Keys starting with "_" are comments and are ignored
by the tests. The file is read once per process.
"""
import json
import os
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "ACL.json"
_CACHE = {}


def load(path=None):
    path = Path(path or os.environ.get("POC_PARAMS") or DEFAULT_PATH)
    key = str(path)
    if key not in _CACHE:
        try:
            _CACHE[key] = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"cannot read test parameters {path}: {exc}") from exc
    return _CACHE[key]
