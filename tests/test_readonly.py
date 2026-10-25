"""Read-only enforcement: no write endpoint can be reached, and no production module even mentions one."""

import re
from pathlib import Path

import pytest

from mmm.broker.readonly import ALLOWED_METHODS, FORBIDDEN_METHODS, ReadOnlyKite, WriteOperationBlocked


class FakeKite:
    def __init__(self):
        self.calls = []

    def profile(self):
        self.calls.append("profile")
        return {"user_id": "X"}

    def place_order(self, **kw):  # must never be reachable
        raise AssertionError("place_order was invoked")

    def quote(self, keys):
        return {k: {"last_price": 1.0} for k in keys}


def test_allowed_method_passes_through():
    ro = ReadOnlyKite(FakeKite(), min_interval_s=0)
    assert ro.profile() == {"user_id": "X"}


@pytest.mark.parametrize("name", sorted(FORBIDDEN_METHODS))
def test_forbidden_methods_blocked(name):
    ro = ReadOnlyKite(FakeKite(), min_interval_s=0)
    with pytest.raises(WriteOperationBlocked):
        getattr(ro, name)


def test_unknown_method_rejected():
    ro = ReadOnlyKite(FakeKite(), min_interval_s=0)
    with pytest.raises(AttributeError):
        ro.some_new_method


def test_allowlist_and_forbidden_disjoint():
    assert not (ALLOWED_METHODS & FORBIDDEN_METHODS)


def test_quotes_chunking():
    ro = ReadOnlyKite(FakeKite(), min_interval_s=0)
    keys = [f"NFO:X{i}" for i in range(1200)]
    out = ro.quotes(keys)
    assert len(out) == 1200


def test_no_source_file_calls_order_endpoints():
    """Static scan: production code must not call any trading write endpoint."""
    root = Path(__file__).resolve().parents[1] / "mmm"
    pattern = re.compile(r"\.(" + "|".join(sorted(FORBIDDEN_METHODS)) + r")\s*\(")
    offenders = []
    for p in root.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        for m in pattern.finditer(text):
            offenders.append(f"{p.relative_to(root)}: {m.group(0)}")
    assert offenders == [], offenders
