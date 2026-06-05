"""Tests for universe reconciliation: scanner drop-off + closed_today policy.

Uses asyncio.run() — no pytest-asyncio dependency.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from live.feed.universe import UniverseManager
from live.session_clock import SessionClock


def _build_mgr() -> UniverseManager:
    """Construct a minimal UniverseManager without running __init__ side effects."""
    mgr = UniverseManager.__new__(UniverseManager)
    mgr._universe = {}
    mgr._closed_today = set()
    mgr._risk_state = MagicMock()
    mgr._risk_state.has_position = lambda t: False
    mgr._risk_state.open_positions = {}
    mgr._risk_state.theoretical_equity = 1000.0
    mgr._ws_send_queue = asyncio.Queue()
    mgr._heartbeat = MagicMock()
    mgr._clock = SessionClock()
    mgr._telegram = None
    mgr._current_ws = None
    return mgr


def _patch_export(monkeypatch):
    import live.feed.universe as uni_mod
    monkeypatch.setattr(uni_mod, "_export_ticker_session", AsyncMock())


# ── handle_snapshot_dropoffs ──────────────────────────────────────────────────

def test_dropoff_removes_missing_no_position(monkeypatch):
    """MSTR drops out of snapshot → removed. SOUN stays (in snapshot)."""
    _patch_export(monkeypatch)
    mgr = _build_mgr()
    mgr._universe = {"MSTR": MagicMock(), "SOUN": MagicMock()}
    mgr.remove_ticker = AsyncMock()

    asyncio.run(mgr.handle_snapshot_dropoffs({"SOUN", "INDO"}))

    mgr.remove_ticker.assert_awaited_once_with("MSTR", close_reason="scanner_dropoff")


def test_dropoff_keeps_ticker_with_position(monkeypatch):
    """Ticker with open position is NOT removed even if absent from snapshot."""
    _patch_export(monkeypatch)
    mgr = _build_mgr()
    mgr._universe = {"AAPL": MagicMock()}
    mgr._risk_state.has_position = lambda t: t == "AAPL"
    mgr.remove_ticker = AsyncMock()

    asyncio.run(mgr.handle_snapshot_dropoffs({"INDO"}))

    mgr.remove_ticker.assert_not_called()


def test_dropoff_handles_empty_universe(monkeypatch):
    """No-op when universe is empty."""
    _patch_export(monkeypatch)
    mgr = _build_mgr()
    mgr.remove_ticker = AsyncMock()

    asyncio.run(mgr.handle_snapshot_dropoffs({"SOUN", "INDO"}))

    mgr.remove_ticker.assert_not_called()


def test_dropoff_multiple_absent_tickers(monkeypatch):
    """All absent tickers without position are removed."""
    _patch_export(monkeypatch)
    mgr = _build_mgr()
    mgr._universe = {"MSTR": MagicMock(), "SOUN": MagicMock(), "INDO": MagicMock()}
    mgr.remove_ticker = AsyncMock()

    asyncio.run(mgr.handle_snapshot_dropoffs({"INDO"}))

    assert mgr.remove_ticker.await_count == 2
    removed = {call.args[0] for call in mgr.remove_ticker.await_args_list}
    assert removed == {"MSTR", "SOUN"}


# ── remove_ticker closed_today policy ─────────────────────────────────────────

def test_remove_scanner_dropoff_not_in_closed_today(monkeypatch):
    """scanner_dropoff removal must NOT add to closed_today (ticker can re-enter)."""
    _patch_export(monkeypatch)
    mgr = _build_mgr()
    ctx = MagicMock()
    ctx.signal_state.intraday_pct = 0.5
    mgr._universe = {"MSTR": ctx}

    asyncio.run(mgr.remove_ticker("MSTR", close_reason="scanner_dropoff"))

    assert "MSTR" not in mgr._closed_today
    assert "MSTR" not in mgr._universe


def test_remove_session_close_not_in_closed_today(monkeypatch):
    """closed_today lockout disabled: session_close removal must NOT add to closed_today."""
    _patch_export(monkeypatch)
    mgr = _build_mgr()
    ctx = MagicMock()
    ctx.signal_state.intraday_pct = 0.5
    mgr._universe = {"MSTR": ctx}

    asyncio.run(mgr.remove_ticker("MSTR", close_reason="session_close"))

    assert "MSTR" not in mgr._closed_today


def test_remove_epg_close_not_in_closed_today(monkeypatch):
    """closed_today lockout disabled: EPG_CLOSE removal must NOT add to closed_today."""
    _patch_export(monkeypatch)
    mgr = _build_mgr()
    ctx = MagicMock()
    ctx.signal_state.intraday_pct = 0.5
    mgr._universe = {"MSTR": ctx}

    asyncio.run(mgr.remove_ticker("MSTR", close_reason="EPG_CLOSE"))

    assert "MSTR" not in mgr._closed_today


# ── Re-entry after drop-off ───────────────────────────────────────────────────

def test_dropped_ticker_can_re_enter(monkeypatch):
    """After scanner_dropoff, ticker is NOT in closed_today, so add path won't skip it."""
    _patch_export(monkeypatch)
    mgr = _build_mgr()
    ctx = MagicMock()
    ctx.signal_state.intraday_pct = 0.5
    mgr._universe = {"MSTR": ctx}

    # Drop off
    asyncio.run(mgr.remove_ticker("MSTR", close_reason="scanner_dropoff"))
    assert "MSTR" not in mgr._closed_today

    # The _add_ticker idempotency check would now ALLOW MSTR back in
    # (since it's neither in universe nor in closed_today)
    add_would_skip = ("MSTR" in mgr._universe) or ("MSTR" in mgr._closed_today)
    assert add_would_skip is False
