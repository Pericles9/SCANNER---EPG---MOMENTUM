"""Tests for quartile-gate REMOVAL (SlopeGate F_ss core swap).

The scanner quartile gate — both the original Q3/Q4 gate and the Q1/Q2
peak-hours override (`_evaluate_entry_gate` / `peak_hours_only`) — has been
removed. Entry selection now belongs to the setup filter. Every eligible name
clearing the gap threshold is admitted at all hours, regardless of quartile.
scanner_quartile is still computed and stored as an analysis field.
"""
from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock

import pytest

import live.scanner_monitor as sm
from live.config import CFG
from live.scanner_monitor import ScannerContext, SnapshotRecord, _poll_once


# ── The quartile gate and its config are gone ─────────────────────────────────

def test_quartile_gate_function_removed():
    """_evaluate_entry_gate / is_peak_hours no longer exist — gate is removed."""
    assert not hasattr(sm, "_evaluate_entry_gate")
    assert not hasattr(sm, "is_peak_hours")


def test_quartile_config_keys_removed():
    """trade_quartiles and peak_hours_only are no longer part of the scanner config."""
    assert not hasattr(CFG.scanner, "trade_quartiles")
    assert not hasattr(CFG.scanner, "peak_hours_only")


# ── Q1/Q2 names clearing the gap are admitted (task acceptance test) ──────────

def test_q1_q2_admitted_after_quartile_gate_removed(monkeypatch):
    """A Q1 and a Q2 ticker that clear the gap are admitted to the universe.

    Previously, with the Q1/Q2 peak-hours override active, these would have been
    rejected outside peak windows. Now they are queued regardless of quartile.
    """
    q1 = ScannerContext("Q1T", 187.0, 1, 12, 0.42, 1, 1_000_000_000)
    q2 = ScannerContext("Q2T", 95.0, 2, 12, 0.30, 2, 1_000_000_000)
    # n_qualifying=0 short-circuits the DB write so we need no DB pool.
    record = SnapshotRecord(1_000_000_000, date.today(), 0, None, "[]")

    async def _fake_snapshot(http, api_key):
        return [q1, q2], record

    monkeypatch.setattr(sm, "build_snapshot_context", _fake_snapshot)
    monkeypatch.setattr(sm.market_status, "refresh", AsyncMock())

    universe_queue: asyncio.Queue = asyncio.Queue()
    asyncio.run(_poll_once(
        http=None,
        universe_queue=universe_queue,
        api_key="x",
        closed_today=set(),
        universe_mgr=None,
    ))

    queued = []
    while not universe_queue.empty():
        ticker, _ctx = universe_queue.get_nowait()
        queued.append(ticker)

    assert "Q1T" in queued, "Q1 ticker clearing the gap must be admitted"
    assert "Q2T" in queued, "Q2 ticker clearing the gap must be admitted"


def test_all_quartiles_admitted_regardless_of_hour(monkeypatch):
    """One ticker per quartile, all admitted — no quartile filter, no hours cap."""
    contexts = [
        ScannerContext("Q1T", 100.0, 1, 8, 0.5, 1, 1_000_000_000),
        ScannerContext("Q2T", 80.0, 2, 8, 0.4, 2, 1_000_000_000),
        ScannerContext("Q3T", 60.0, 3, 8, 0.3, 3, 1_000_000_000),
        ScannerContext("Q4T", 40.0, 4, 8, 0.2, 4, 1_000_000_000),
    ]
    record = SnapshotRecord(1_000_000_000, date.today(), 0, None, "[]")

    async def _fake_snapshot(http, api_key):
        return contexts, record

    monkeypatch.setattr(sm, "build_snapshot_context", _fake_snapshot)
    monkeypatch.setattr(sm.market_status, "refresh", AsyncMock())

    universe_queue: asyncio.Queue = asyncio.Queue()
    asyncio.run(_poll_once(
        http=None,
        universe_queue=universe_queue,
        api_key="x",
        closed_today=set(),
        universe_mgr=None,
    ))

    queued = set()
    while not universe_queue.empty():
        ticker, _ctx = universe_queue.get_nowait()
        queued.add(ticker)

    assert queued == {"Q1T", "Q2T", "Q3T", "Q4T"}


def test_closed_today_still_blocks_admission(monkeypatch):
    """Removing the quartile gate must NOT disturb the closed_today lockout."""
    q1 = ScannerContext("DONE", 120.0, 1, 4, 0.6, 1, 1_000_000_000)
    record = SnapshotRecord(1_000_000_000, date.today(), 0, None, "[]")

    async def _fake_snapshot(http, api_key):
        return [q1], record

    monkeypatch.setattr(sm, "build_snapshot_context", _fake_snapshot)
    monkeypatch.setattr(sm.market_status, "refresh", AsyncMock())

    universe_queue: asyncio.Queue = asyncio.Queue()
    asyncio.run(_poll_once(
        http=None,
        universe_queue=universe_queue,
        api_key="x",
        closed_today={"DONE"},
        universe_mgr=None,
    ))

    assert universe_queue.empty(), "Ticker in closed_today must not be re-admitted"


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
