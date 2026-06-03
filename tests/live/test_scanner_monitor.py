"""Tests for live.scanner_monitor — dataclass correctness and ET helper.

The quartile / peak-hours entry gate has been removed (SlopeGate F_ss core swap);
its dedicated tests now live in test_quartile_gate.py. No network calls.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from live.config import CFG
from live.scanner_monitor import (
    ScannerContext,
    SnapshotRecord,
    get_now_et,
)

_ET = ZoneInfo("America/New_York")


def _et(hour: int, minute: int = 0, second: int = 0) -> datetime:
    """Build a tz-aware datetime at the given ET time (today's date)."""
    from datetime import date
    d = date.today()
    return datetime(d.year, d.month, d.day, hour, minute, second, tzinfo=_ET)


# ── ScannerContext / SnapshotRecord ───────────────────────────────────────────

def test_scanner_context_fields():
    ctx = ScannerContext(
        ticker="TSLA",
        pct_change=45.2,
        scanner_rank=1,
        scanner_n=12,
        scanner_heat=0.31,
        scanner_quartile=1,
        snapshot_ns=1_000_000_000,
    )
    assert ctx.ticker == "TSLA"
    assert ctx.pct_change == 45.2
    assert ctx.scanner_quartile == 1
    assert ctx.snapshot_ns == 1_000_000_000


def test_snapshot_record_fields():
    from datetime import date
    rec = SnapshotRecord(
        snapshot_ns=9_999,
        session_date=date(2026, 5, 21),
        n_qualifying=5,
        heat_p75=38.7,
        snapshot_json="[]",
    )
    assert rec.n_qualifying == 5
    assert rec.heat_p75 == pytest.approx(38.7)
    assert rec.snapshot_json == "[]"


def test_get_now_et_is_eastern():
    dt = get_now_et()
    assert dt.tzinfo is not None
    # tzinfo should be US/Eastern (ZoneInfo)
    from zoneinfo import ZoneInfo
    assert dt.tzinfo == ZoneInfo("America/New_York")
