"""Startup DB-state reconstruction tests (Telegram continuity across restarts)."""
from __future__ import annotations

import asyncio
import inspect
from datetime import date

import pytest

from live.orders.risk import RiskState
from live.recovery.state_recovery import reconstruct_daily_state


class _FakeConn:
    def __init__(self, daily, hist, theo, raise_on_query=False):
        self._daily = daily
        self._hist = hist
        self._theo = theo
        self._raise = raise_on_query

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def fetchrow(self, sql, *args):
        if self._raise:
            raise RuntimeError("db down")
        return self._daily

    async def fetch(self, sql, *args):
        return self._hist

    async def fetchval(self, sql, *args):
        return self._theo


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return self._conn


def _rs(max_daily_loss=-500.0, theo_seed=10000.0):
    rs = RiskState()
    rs.max_daily_loss = max_daily_loss
    rs.theoretical_equity = theo_seed
    return rs


def _run(rs, conn):
    asyncio.run(reconstruct_daily_state(
        rs, _FakePool(conn), date.today(), "scanner_vwap", kelly_lookback=50,
    ))


def test_reconstructs_daily_pnl_and_kelly_history():
    conn = _FakeConn(
        daily={"pnl": 123.45, "n": 3},
        # DB returns newest-first (ORDER BY exit_ns DESC); helper reverses to oldest-first.
        hist=[{"pnl_pct": 0.03}, {"pnl_pct": -0.02}, {"pnl_pct": 0.01}],
        theo=12345.0,
    )
    rs = _rs()
    _run(rs, conn)
    assert rs.daily_pnl == pytest.approx(123.45)
    assert rs._trade_history == [0.01, -0.02, 0.03]      # oldest → newest
    assert rs.theoretical_equity == pytest.approx(12345.0)
    assert rs._loss_limit_hit is False


def test_loss_limit_rearmed_when_past_limit():
    conn = _FakeConn(daily={"pnl": -600.0, "n": 5}, hist=[], theo=None)
    rs = _rs(max_daily_loss=-500.0)
    _run(rs, conn)
    assert rs.daily_pnl == pytest.approx(-600.0)
    assert rs._loss_limit_hit is True


def test_null_theo_keeps_equity_seed():
    conn = _FakeConn(daily={"pnl": 10.0, "n": 1}, hist=[], theo=None)
    rs = _rs(theo_seed=9999.0)
    _run(rs, conn)
    assert rs.theoretical_equity == pytest.approx(9999.0)   # untouched


def test_empty_db_starts_flat():
    conn = _FakeConn(daily={"pnl": 0.0, "n": 0}, hist=[], theo=None)
    rs = _rs()
    _run(rs, conn)
    assert rs.daily_pnl == 0.0
    assert rs._trade_history == []
    assert rs._loss_limit_hit is False


def test_db_error_is_safe():
    """A DB failure must not raise — start flat."""
    conn = _FakeConn(daily=None, hist=None, theo=None, raise_on_query=True)
    rs = _rs()
    rs.daily_pnl = 0.0
    _run(rs, conn)                       # must not raise
    assert rs.daily_pnl == 0.0


def test_handlers_have_no_hardcoded_strategy_id():
    """Regression guard: /trades and /summary must query CFG.strategy_id, never a literal id."""
    import live.bot.handlers as handlers
    src = inspect.getsource(handlers)
    assert '"epg_v1"' not in src and "'epg_v1'" not in src
    assert "CFG.strategy_id" in src
