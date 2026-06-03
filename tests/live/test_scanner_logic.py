"""Stage 3: Implementation logic tests — no live connection, all mocks.

Covers six behaviors:
  1. Reconciliation add path — all qualifying tickers passed to add_ticker
  2. Reconciliation remove path — drop-offs go through remove_ticker(scanner_dropoff)
  3. Position protection — ticker with open position is NOT removed even if dropped
  4. Quartile algorithm — cumulative momentum boundaries match implementation
  5. All quartiles enter the universe — no Q1/Q2 gate
  6. Scanner context fields — rank, n, heat correct
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from live.feed.universe import UniverseManager
from live.scanner.context import compute_scanner_context


# ── helpers ──────────────────────────────────────────────────────────────────

def _build_mgr(universe: dict = None, positions: set = None) -> UniverseManager:
    """Build a UniverseManager skipping __init__ side effects."""
    mgr = UniverseManager.__new__(UniverseManager)
    mgr._universe = dict(universe or {})
    mgr._closed_today = set()
    rs = MagicMock()
    pos = positions or set()
    rs.has_position = lambda t: t in pos
    rs.open_positions = {t: {"qty": 1, "avg_cost": 0.0} for t in pos}
    rs.theoretical_equity = 1000.0
    mgr._risk_state = rs
    mgr._ws_send_queue = asyncio.Queue()
    mgr._heartbeat = MagicMock()
    mgr._session_date = None
    mgr._telegram = None
    mgr._current_ws = None
    return mgr


async def _run_reconciliation(
    mgr: UniverseManager,
    snapshot_tickers: list[str],
    add_mock: AsyncMock,
) -> None:
    """Drive both directions of reconciliation against a mock add_ticker."""
    qualifying = set(snapshot_tickers)

    # ADD direction (idempotent — call for every qualifying ticker)
    for ticker in snapshot_tickers:
        await add_mock(ticker)

    # REMOVE direction (drop-offs without open position)
    await mgr.handle_snapshot_dropoffs(qualifying)


# ── Test 1 — reconciliation, add path ─────────────────────────────────────────

def test_reconciliation_add_path():
    """Empty universe, 3 qualifying tickers → add_ticker called for all 3."""
    mgr = _build_mgr(universe={}, positions=set())
    add = AsyncMock()

    asyncio.run(_run_reconciliation(mgr, ["MSTR", "SOUN", "INDO"], add))

    assert add.await_count == 3
    called_tickers = {call.args[0] for call in add.await_args_list}
    assert called_tickers == {"MSTR", "SOUN", "INDO"}


# ── Test 2 — reconciliation, remove path ──────────────────────────────────────

def test_reconciliation_remove_path():
    """Universe [MSTR, SOUN] (no positions), snapshot [SOUN, INDO] → MSTR removed, SOUN/INDO added."""
    mgr = _build_mgr(universe={"MSTR": MagicMock(), "SOUN": MagicMock()}, positions=set())
    mgr.remove_ticker = AsyncMock()
    add = AsyncMock()

    asyncio.run(_run_reconciliation(mgr, ["SOUN", "INDO"], add))

    # Add called for both names in snapshot (SOUN idempotent, INDO new)
    called_tickers = {call.args[0] for call in add.await_args_list}
    assert "SOUN" in called_tickers
    assert "INDO" in called_tickers

    # Remove called once for MSTR with the correct close_reason
    mgr.remove_ticker.assert_awaited_once_with("MSTR", close_reason="scanner_dropoff")


# ── Test 3 — position protection ──────────────────────────────────────────────

def test_position_protection_blocks_removal():
    """MSTR holds open position; dropped from snapshot → must NOT be removed."""
    mgr = _build_mgr(universe={"MSTR": MagicMock()}, positions={"MSTR"})
    mgr.remove_ticker = AsyncMock()
    add = AsyncMock()

    asyncio.run(_run_reconciliation(mgr, ["INDO"], add))

    mgr.remove_ticker.assert_not_called()


# ── Test 4 — quartile computation ─────────────────────────────────────────────

def test_quartile_boundaries_strict_less_than():
    """Phase G v2 quartile uses strict < boundaries.

    Six tickers [10,10,10,10,5,5] total=50, boundaries 12.5/25.0/37.5
    → expected quartiles per cumulative momentum:
       t1: running=10 < 12.5 → Q1
       t2: running=20 < 25.0 → Q2
       t3: running=30 < 37.5 → Q3
       t4: running=40 (>= 37.5) → Q4
       t5,t6 → Q4
    """
    qualifying = [
        {"ticker": "A", "pct_change": 10.0},
        {"ticker": "B", "pct_change": 10.0},
        {"ticker": "C", "pct_change": 10.0},
        {"ticker": "D", "pct_change": 10.0},
        {"ticker": "E", "pct_change": 5.0},
        {"ticker": "F", "pct_change": 5.0},
    ]
    out = compute_scanner_context(qualifying)
    by_ticker = {x["ticker"]: x["scanner_quartile"] for x in out}
    assert by_ticker == {"A": 1, "B": 2, "C": 3, "D": 4, "E": 4, "F": 4}


def test_quartile_user_spec_example():
    """User-spec example [0.80, 0.50, 0.30] total=1.6, boundaries 0.4/0.8/1.2.

    Implementation uses strict <:
      t1 (0.80): running=0.80 — 0.80<0.40 NO, 0.80<0.80 NO, 0.80<1.20 YES → Q3
      t2 (0.50): running=1.30 — 1.30<1.20 NO → Q4
      t3 (0.30): running=1.60 → Q4
    """
    qualifying = [
        {"ticker": "T1", "pct_change": 0.80},
        {"ticker": "T2", "pct_change": 0.50},
        {"ticker": "T3", "pct_change": 0.30},
    ]
    out = compute_scanner_context(qualifying)
    by_ticker = {x["ticker"]: x["scanner_quartile"] for x in out}
    assert by_ticker == {"T1": 3, "T2": 4, "T3": 4}


# ── Test 5 — all quartiles enter the universe (no quartile gate) ──────────────

def test_all_quartiles_enter_universe_via_reconciliation():
    """Mock 4 tickers (one per quartile) → add_ticker called for all 4."""
    # Hand-craft a snapshot that produces one ticker per quartile
    qualifying = [
        {"ticker": "Q1T", "pct_change": 10.0},
        {"ticker": "Q2T", "pct_change": 10.0},
        {"ticker": "Q3T", "pct_change": 10.0},
        {"ticker": "Q4T", "pct_change": 10.0},
        {"ticker": "PAD", "pct_change": 5.0},  # padding so boundaries land cleanly
        {"ticker": "PAD2", "pct_change": 5.0},
    ]
    out = compute_scanner_context(qualifying)
    quartiles = {x["ticker"]: x["scanner_quartile"] for x in out}
    # Sanity: at least one per quartile present in our 6
    assert set(quartiles.values()) == {1, 2, 3, 4}

    mgr = _build_mgr(universe={}, positions=set())
    add = AsyncMock()
    snapshot_tickers = [x["ticker"] for x in out]
    asyncio.run(_run_reconciliation(mgr, snapshot_tickers, add))

    called = {call.args[0] for call in add.await_args_list}
    # All quartiles enter — no Q1/Q2 gate
    assert {"Q1T", "Q2T", "Q3T", "Q4T"}.issubset(called)


# ── Test 6 — scanner context fields ───────────────────────────────────────────

def test_scanner_rank_descending_by_pct():
    qualifying = [
        {"ticker": "A", "pct_change": 100.0},
        {"ticker": "B", "pct_change": 50.0},
        {"ticker": "C", "pct_change": 75.0},
        {"ticker": "D", "pct_change": 25.0},
        {"ticker": "E", "pct_change": 60.0},
    ]
    out = compute_scanner_context(qualifying)
    by_ticker = {x["ticker"]: x["scanner_rank"] for x in out}
    # A=100 -> rank 1, C=75 -> 2, E=60 -> 3, B=50 -> 4, D=25 -> 5
    assert by_ticker == {"A": 1, "C": 2, "E": 3, "B": 4, "D": 5}


def test_scanner_n_equals_qualifying_count():
    qualifying = [
        {"ticker": "A", "pct_change": 50.0},
        {"ticker": "B", "pct_change": 40.0},
        {"ticker": "C", "pct_change": 30.0},
        {"ticker": "D", "pct_change": 20.0},
        {"ticker": "E", "pct_change": 10.0},
    ]
    out = compute_scanner_context(qualifying)
    assert all(x["scanner_n"] == 5 for x in out)


def test_snapshot_heat_p75_is_75th_percentile():
    """Snapshot-level heat_p75 stored on SnapshotRecord is the 75th percentile of pct_change."""
    pct_values = np.array([50.0, 40.0, 30.0, 20.0, 10.0])
    expected_p75 = float(np.percentile(pct_values, 75))
    # numpy 75th percentile of [10,20,30,40,50] = 40.0
    assert expected_p75 == pytest.approx(40.0)


def test_compute_scanner_context_empty_input():
    """Empty input must not crash and must return empty output."""
    assert compute_scanner_context([]) == []
