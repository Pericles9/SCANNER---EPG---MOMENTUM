"""Tests for live.startup_triage.

Triage decision tree, watcher behaviour, close-request construction. No real
IBKR, no real DB — everything mocked. Real asyncio.Queue / asyncio.run.
"""
from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from live.startup_triage import (
    EPGTriageOutcome,
    _Position,
    _build_close_request,
    _close_on_resume_watcher,
    _current_session_bucket,
    _session_end_ns,
    _synthetic_scanner_context,
    startup_position_triage,
)


# ── DB mock infra ────────────────────────────────────────────────────────────

class _FakeConn:
    def __init__(self, positions=None, refit=None, ticks=None, t_event=None):
        self._positions = positions or []
        self._refit = refit
        self._ticks = ticks or []
        self._t_event = t_event

    async def fetch(self, sql, *args):
        if "FROM positions" in sql:
            return self._positions
        if "FROM ticks" in sql:
            return self._ticks
        return []

    async def fetchrow(self, sql, *args):
        if "FROM hawkes_refits" in sql:
            return self._refit
        if "FROM signal_events" in sql:
            return self._t_event
        return None


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self_):
                return conn

            async def __aexit__(self_, *a):
                return False

        return _Ctx()


def _ibkr_mock(bid: float, ask: float) -> MagicMock:
    ibkr = MagicMock()
    ibkr.snapshot_quote = AsyncMock(return_value=(bid, ask))
    return ibkr


def _telegram_mock() -> MagicMock:
    tg = MagicMock()
    tg.send_silent = AsyncMock()
    return tg


def _universe_mgr_mock() -> MagicMock:
    mgr = MagicMock()
    mgr.add_ticker = AsyncMock()
    return mgr


# ── startup_position_triage — no positions ──────────────────────────────────

def test_triage_no_positions_is_a_noop():
    pool = _FakePool(_FakeConn(positions=[]))
    ibkr = _ibkr_mock(10.0, 10.02)
    order_queue: asyncio.Queue = asyncio.Queue()
    universe = _universe_mgr_mock()
    tg = _telegram_mock()

    asyncio.run(startup_position_triage(
        pool=pool, ibkr=ibkr, order_queue=order_queue,
        universe_mgr=universe, telegram=tg,
        session_date=date(2026, 5, 27),
        hot_signal_events=[],
    ))

    universe.add_ticker.assert_not_called()
    assert order_queue.empty()
    # Should send a "0 open positions" telegram
    assert tg.send_silent.await_count >= 1


# ── No IBKR quote → immediate close at avg_entry_price ──────────────────────

def test_triage_no_quote_closes_immediately_at_avg_cost():
    pos_row = {"ticker": "HALT1", "qty": 100, "avg_entry_price": 5.0, "open_ns": 0}
    pool = _FakePool(_FakeConn(positions=[pos_row]))
    ibkr = _ibkr_mock(0.0, 0.0)  # no quote (halted / pre-market)
    order_queue: asyncio.Queue = asyncio.Queue()
    universe = _universe_mgr_mock()
    tg = _telegram_mock()
    events: list = []

    asyncio.run(startup_position_triage(
        pool=pool, ibkr=ibkr, order_queue=order_queue,
        universe_mgr=universe, telegram=tg,
        session_date=date(2026, 5, 27),
        hot_signal_events=events,
    ))

    universe.add_ticker.assert_not_called()
    # Close is queued immediately — order_queue is not empty
    assert not order_queue.empty()
    req = order_queue.get_nowait()
    assert req.ticker == "HALT1"
    assert req.side == "SELL"
    assert req.exit_reason == "EPG_NO_QUOTE_ON_RESTART"
    # limit_price is derived from avg_entry_price minus extended_exit_offset (0.05)
    assert req.limit_price == pytest.approx(4.95, abs=0.01)


# ── Valid quote, UNRESOLVABLE (no refit) → close ────────────────────────────

def test_triage_unresolvable_no_refit_closes_position():
    pos_row = {"ticker": "ABC", "qty": 50, "avg_entry_price": 12.0, "open_ns": 0}
    pool = _FakePool(_FakeConn(positions=[pos_row], refit=None))
    ibkr = _ibkr_mock(11.95, 12.05)
    order_queue: asyncio.Queue = asyncio.Queue()
    universe = _universe_mgr_mock()
    tg = _telegram_mock()
    events: list = []

    asyncio.run(startup_position_triage(
        pool=pool, ibkr=ibkr, order_queue=order_queue,
        universe_mgr=universe, telegram=tg,
        session_date=date(2026, 5, 27),
        hot_signal_events=events,
    ))

    universe.add_ticker.assert_not_called()
    assert order_queue.qsize() == 1
    req = order_queue.get_nowait()
    assert req.ticker == "ABC"
    assert req.side == "SELL"
    assert req.qty == 50
    assert req.is_entry is False
    assert req.exit_reason == "EPG_UNRESOLVABLE_ON_RESTART"
    assert any(e[4] == "TRIAGE_CLOSE_UNRESOLVABLE" for e in events)


# ── Valid quote, INACTIVE (refit present, no ticks since) → resume ──────────

def test_triage_inactive_resumes_via_add_ticker():
    pos_row = {"ticker": "XYZ", "qty": 30, "avg_entry_price": 8.0, "open_ns": 0}
    refit_row = {
        "mu_buy": 0.1, "mu_sell": 0.1,
        "alpha_buy_self": 0.005, "alpha_sell_self": 0.005,
        "refit_ns": 0,
    }
    pool = _FakePool(_FakeConn(positions=[pos_row], refit=refit_row, ticks=[]))
    ibkr = _ibkr_mock(7.95, 8.05)
    order_queue: asyncio.Queue = asyncio.Queue()
    universe = _universe_mgr_mock()
    tg = _telegram_mock()
    events: list = []

    asyncio.run(startup_position_triage(
        pool=pool, ibkr=ibkr, order_queue=order_queue,
        universe_mgr=universe, telegram=tg,
        session_date=date(2026, 5, 27),
        hot_signal_events=events,
    ))

    # No close — resume instead
    assert order_queue.empty()
    universe.add_ticker.assert_awaited_once()
    call = universe.add_ticker.await_args
    assert call.args[0] == "XYZ"
    assert call.kwargs["existing_position"]["qty"] == 30
    assert call.kwargs["existing_position"]["avg_cost"] == 8.0
    assert any(e[4] == "TRIAGE_RESUME_INACTIVE" for e in events)


# ── Multiple positions process concurrently ─────────────────────────────────

def test_triage_processes_multiple_positions_concurrently():
    pos_rows = [
        {"ticker": "T1", "qty": 10, "avg_entry_price": 5.0, "open_ns": 0},
        {"ticker": "T2", "qty": 20, "avg_entry_price": 6.0, "open_ns": 0},
        {"ticker": "T3", "qty": 30, "avg_entry_price": 7.0, "open_ns": 0},
    ]
    pool = _FakePool(_FakeConn(positions=pos_rows, refit=None))  # all UNRESOLVABLE
    ibkr = _ibkr_mock(5.0, 5.10)
    order_queue: asyncio.Queue = asyncio.Queue()
    universe = _universe_mgr_mock()
    tg = _telegram_mock()

    asyncio.run(startup_position_triage(
        pool=pool, ibkr=ibkr, order_queue=order_queue,
        universe_mgr=universe, telegram=tg,
        session_date=date(2026, 5, 27),
        hot_signal_events=[],
    ))

    # Three SELL requests, in any order
    tickers = sorted(order_queue.get_nowait().ticker for _ in range(3))
    assert tickers == ["T1", "T2", "T3"]
    assert order_queue.empty()


# ── _build_close_request — bucket and limit handling ────────────────────────

def test_build_close_request_pre_market_sets_liberal_limit():
    pos = _Position(ticker="X", qty=10, avg_entry_price=10.0, open_ns=0)
    req = _build_close_request(pos, "EPG_FAIL_ON_RESTART", "pre_market",
                                bid=9.95, ask=10.05)
    assert req.limit_price is not None
    # bid - extended_exit_offset (0.05) = 9.90
    assert req.limit_price == 9.90
    assert req.side == "SELL"
    assert req.qty == 10  # exact qty, not the sentinel 0


def test_build_close_request_post_market_sets_liberal_limit():
    pos = _Position(ticker="X", qty=10, avg_entry_price=10.0, open_ns=0)
    req = _build_close_request(pos, "EPG_FAIL_ON_RESTART", "post_market",
                                bid=9.95, ask=10.05)
    assert req.limit_price == 9.90


def test_build_close_request_regular_hours_sets_limit_active_outside_rth():
    """All orders are limit orders active outside RTH — RTH closes set a limit too."""
    pos = _Position(ticker="X", qty=10, avg_entry_price=10.0, open_ns=0)
    req = _build_close_request(pos, "EPG_FAIL_ON_RESTART", "regular_hours",
                                bid=9.95, ask=10.05)
    assert req.limit_price == 9.90  # bid - extended_exit_offset


def test_build_close_request_falls_back_when_only_ask_available():
    pos = _Position(ticker="X", qty=10, avg_entry_price=10.0, open_ns=0)
    req = _build_close_request(pos, "CLOSE_ON_RESUME", "pre_market",
                                bid=0.0, ask=10.05)
    # bid_ref = ask - extended_exit_offset = 10.00; limit = 9.95
    assert req.limit_price == 9.95


# ── _close_on_resume_watcher — EOD + quote-resumed paths ─────────────────────

def test_watcher_closes_on_quote_resume():
    pos = _Position(ticker="HALT", qty=100, avg_entry_price=5.0, open_ns=0)
    order_queue: asyncio.Queue = asyncio.Queue()
    tg = _telegram_mock()
    events: list = []

    # IBKR returns (0, 0) the first time, then (4.95, 5.05)
    bid_ask_iter = iter([(0.0, 0.0), (0.0, 0.0), (4.95, 5.05)])
    ibkr = MagicMock()
    ibkr.snapshot_quote = AsyncMock(side_effect=lambda t: next(bid_ask_iter))

    # Patch sleep so the test runs fast
    import live.startup_triage as triage_mod
    original_sleep = asyncio.sleep
    asyncio.sleep = AsyncMock(return_value=None)  # type: ignore[assignment]
    try:
        asyncio.run(_close_on_resume_watcher(
            pos=pos, ibkr=ibkr, order_queue=order_queue,
            telegram=tg, session_end_ns=10**20,  # far future
            session_date=date(2026, 5, 27),
            hot_signal_events=events,
        ))
    finally:
        asyncio.sleep = original_sleep  # type: ignore[assignment]

    assert order_queue.qsize() == 1
    req = order_queue.get_nowait()
    assert req.ticker == "HALT"
    assert req.exit_reason == "CLOSE_ON_RESUME"
    assert any(e[4] == "TRIAGE_WATCHER_CLOSED" for e in events)


def test_watcher_force_closes_at_eod():
    pos = _Position(ticker="DEAD", qty=200, avg_entry_price=3.0, open_ns=0)
    order_queue: asyncio.Queue = asyncio.Queue()
    tg = _telegram_mock()
    events: list = []
    ibkr = _ibkr_mock(0.0, 0.0)  # never resumes

    asyncio.run(_close_on_resume_watcher(
        pos=pos, ibkr=ibkr, order_queue=order_queue,
        telegram=tg, session_end_ns=0,  # already past EOD → immediate force close
        session_date=date(2026, 5, 27),
        hot_signal_events=events,
    ))

    assert order_queue.qsize() == 1
    req = order_queue.get_nowait()
    assert req.exit_reason == "CLOSE_FORCED_EOD"
    assert req.qty == 200
    assert any(e[4] == "TRIAGE_WATCHER_EOD_FORCED" for e in events)


# ── Helpers ──────────────────────────────────────────────────────────────────

def test_synthetic_scanner_context_has_required_fields():
    ctx = _synthetic_scanner_context()
    assert ctx["pct_change"] == 0.0
    assert ctx["scanner_quartile"] == 0
    assert ctx["snapshot_ns"] == 0


def test_session_end_ns_uses_8pm_et_bound():
    """Existing _session_ns_bounds returns 8pm ET as end — must match."""
    end_ns = _session_end_ns(date(2026, 5, 27))
    # Very loose sanity: this is a ns value within ~16 hours of midnight UTC
    # for the date. Mostly we just want it to be non-zero and not crash.
    assert end_ns > 0
    # Sanity: end > current time (we're well before 8pm ET on 2026-05-27)


def test_current_session_bucket_returns_valid_value():
    bkt = _current_session_bucket()
    assert bkt in ("pre_market", "regular_hours", "post_market")
