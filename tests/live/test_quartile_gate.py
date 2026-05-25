"""Tests for quartile-gate removal — all Q1–Q4 tickers must pass the entry gate.

Locked decision: `todaysChangePerc >= 0.30` — all quartiles traded.
peak_hours_only=False in strategy.json (default). Gate returns True for every quartile.
"""
from __future__ import annotations

from datetime import datetime, date
from zoneinfo import ZoneInfo

import pytest

from live.config import CFG
from live.scanner_monitor import _evaluate_entry_gate, ScannerContext

_ET = ZoneInfo("America/New_York")


def _et(hour: int, minute: int = 0) -> datetime:
    d = date.today()
    return datetime(d.year, d.month, d.day, hour, minute, 0, tzinfo=_ET)


# ── All quartiles admitted when peak_hours_only is False (current locked default) ──

def test_q1_admitted_when_peak_hours_only_false():
    assert CFG.scanner.peak_hours_only is False, "Default must be peak_hours_only=False"
    assert _evaluate_entry_gate(1, _et(10, 0)) is True


def test_q2_admitted_when_peak_hours_only_false():
    assert _evaluate_entry_gate(2, _et(10, 0)) is True


def test_q3_admitted_when_peak_hours_only_false():
    """Previously blocked — now admitted (Fix 2: quartile gate removed)."""
    assert _evaluate_entry_gate(3, _et(10, 0)) is True


def test_q4_admitted_when_peak_hours_only_false():
    """Previously blocked — now admitted (Fix 2: quartile gate removed)."""
    assert _evaluate_entry_gate(4, _et(10, 0)) is True


def test_all_quartiles_admitted_off_peak():
    """No hours cap when peak_hours_only=False — admit all quartiles at all hours."""
    for q in (1, 2, 3, 4):
        # Pre-market
        assert _evaluate_entry_gate(q, _et(7, 0)) is True
        # Midday lull
        assert _evaluate_entry_gate(q, _et(12, 30)) is True
        # Post-market
        assert _evaluate_entry_gate(q, _et(18, 0)) is True


# ── ScannerContext still carries quartile (research field) ────────────────────

def test_scanner_quartile_still_stored_q1():
    """Quartile is still computed/stored even though it's not a gate anymore."""
    ctx = ScannerContext(
        ticker="MSTR", pct_change=187.0,
        scanner_rank=1, scanner_n=12, scanner_heat=0.42,
        scanner_quartile=1, snapshot_ns=1_000_000_000,
    )
    assert ctx.scanner_quartile == 1


def test_scanner_quartile_still_stored_q4():
    ctx = ScannerContext(
        ticker="VERB", pct_change=33.0,
        scanner_rank=8, scanner_n=12, scanner_heat=0.05,
        scanner_quartile=4, snapshot_ns=1_000_000_000,
    )
    assert ctx.scanner_quartile == 4


# ── Legacy peak-hours mode still works when explicitly enabled ────────────────

def test_legacy_peak_hours_mode_rejects_q3(monkeypatch):
    """When peak_hours_only is toggled True (legacy mode), Q3 is rejected during peak."""
    monkeypatch.setattr(CFG.scanner, "peak_hours_only", True)
    assert _evaluate_entry_gate(3, _et(10, 0)) is False


def test_legacy_peak_hours_mode_admits_q1_peak(monkeypatch):
    monkeypatch.setattr(CFG.scanner, "peak_hours_only", True)
    assert _evaluate_entry_gate(1, _et(10, 0)) is True


def test_legacy_peak_hours_mode_rejects_off_peak(monkeypatch):
    """Even Q1 is rejected outside peak windows when legacy mode is active."""
    monkeypatch.setattr(CFG.scanner, "peak_hours_only", True)
    assert _evaluate_entry_gate(1, _et(12, 0)) is False
