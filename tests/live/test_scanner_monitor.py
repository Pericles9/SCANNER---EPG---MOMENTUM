"""Tests for live.scanner_monitor — gate logic and dataclass correctness.

These tests cover the peak-hours gate, quartile gate, and ScannerContext/SnapshotRecord
dataclasses. No network calls; all tests are synchronous or use asyncio.run().
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from live.scanner_monitor import (
    ScannerContext,
    SnapshotRecord,
    _evaluate_entry_gate,
    get_now_et,
    is_peak_hours,
)

_ET = ZoneInfo("America/New_York")


def _et(hour: int, minute: int = 0, second: int = 0) -> datetime:
    """Build a tz-aware datetime at the given ET time (today's date)."""
    from datetime import date
    d = date.today()
    return datetime(d.year, d.month, d.day, hour, minute, second, tzinfo=_ET)


# ── is_peak_hours ─────────────────────────────────────────────────────────────

def test_is_peak_hours_morning_window():
    assert is_peak_hours(_et(10, 15)) is True


def test_is_peak_hours_afternoon_window():
    assert is_peak_hours(_et(14, 45)) is True


def test_is_peak_hours_midday_exclusion():
    assert is_peak_hours(_et(12, 30)) is False


def test_is_peak_hours_pre_market():
    assert is_peak_hours(_et(7, 0)) is False


def test_is_peak_hours_morning_boundary_inclusive():
    # 09:30:00 is included (start <= t)
    assert is_peak_hours(_et(9, 30, 0)) is True


def test_is_peak_hours_morning_boundary_exclusive():
    # 11:30:00 is excluded (t < end)
    assert is_peak_hours(_et(11, 30, 0)) is False


def test_is_peak_hours_afternoon_boundary_inclusive():
    assert is_peak_hours(_et(14, 0, 0)) is True


def test_is_peak_hours_post_close():
    assert is_peak_hours(_et(16, 30)) is False


# ── _evaluate_entry_gate ──────────────────────────────────────────────────────

def test_entry_gate_q1_peak():
    # Q1 during peak hours → admitted
    assert _evaluate_entry_gate(1, _et(10, 0)) is True


def test_entry_gate_q2_peak():
    # Q2 during peak hours → admitted
    assert _evaluate_entry_gate(2, _et(10, 0)) is True


def test_entry_gate_q3_peak_rejected():
    # Q3 during peak hours → rejected (only Q1+Q2 pass)
    assert _evaluate_entry_gate(3, _et(10, 0)) is False


def test_entry_gate_q4_peak_rejected():
    assert _evaluate_entry_gate(4, _et(10, 0)) is False


def test_entry_gate_q1_off_peak_rejected():
    # Q1 outside peak hours → rejected regardless of quartile
    assert _evaluate_entry_gate(1, _et(12, 0)) is False


def test_entry_gate_q2_off_peak_rejected():
    assert _evaluate_entry_gate(2, _et(8, 0)) is False


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
