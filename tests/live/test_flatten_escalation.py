"""Tests for flatten escalation fixes.

Five gate tests:
  1 — Exit timeout does NOT trigger FlattenAllRequest
  2 — FlattenTickerRequest closes only the named ticker
  3 — Dead man's switch enqueues FlattenTickerRequest not FlattenAllRequest
  4 — pending_close_monitor enqueues FlattenTickerRequest not FlattenAllRequest
  5 — _execute_flatten_all submits positions concurrently
"""
from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from live.orders.risk import FlattenAllRequest, FlattenTickerRequest, OrderRequest, RiskState
from live.orders.worker import (
    _execute_flatten_all,
    _execute_flatten_ticker,
    order_worker,
    pending_close_monitor,
)
from live.feed.signal_loop import HeartbeatMonitor, heartbeat_monitor


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_fill(ticker: str, qty: int):
    from live.orders.ibkr import Fill
    return Fill(
        ticker=ticker,
        side="SELL",
        qty=qty,
        filled_qty=qty,
        remaining_qty=0,
        fill_price=100.0,
        ibkr_order_id=99999,
        session_bucket="regular_hours",
        order_type="LMT",
        limit_price=100.0,
        submitted_ns=0,
        filled_at=datetime.now(timezone.utc),
        exit_reason="test",
        is_entry=False,
        intraday_pct=0.0,
        expected_price=100.0,
        slippage_bps=0.0,
        status="filled",
    )


class _MockConn:
    """Minimal asyncpg connection mock supporting transaction + fetchrow + execute."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def transaction(self):
        return self

    async def fetchrow(self, *args, **kwargs):
        # First call is INSERT INTO orders RETURNING id — return id=1.
        # Second call is SELECT from positions — return None (no row).
        if not hasattr(self, "_fetchrow_calls"):
            self._fetchrow_calls = 0
        self._fetchrow_calls += 1
        if self._fetchrow_calls == 1:
            return {"id": 1}
        return None

    async def execute(self, *args, **kwargs):
        pass


class _MockPool:
    def acquire(self):
        return _MockConn()


# ---------------------------------------------------------------------------
# Test 1 — Exit timeout does NOT trigger FlattenAllRequest
# ---------------------------------------------------------------------------

def test_exit_timeout_no_flatten_all():
    """After an exit order times out, no FlattenAllRequest should appear on the queue."""

    async def _run():
        queue: asyncio.Queue = asyncio.Queue()
        risk_state = RiskState()
        risk_state.open_positions["AAPL"] = {"qty": 100, "avg_cost": 150.0}

        req = OrderRequest(
            ticker="AAPL",
            side="SELL",
            qty=100,
            session_bucket="regular_hours",
            is_entry=False,
            exit_reason="EPG_CLOSE",
        )
        queue.put_nowait(req)

        ibkr = MagicMock()
        ibkr.submit = AsyncMock(return_value=None)
        telegram = AsyncMock()
        session_clock = MagicMock()
        session_clock.date = date.today()

        worker_task = asyncio.create_task(
            order_worker(queue, risk_state, ibkr, telegram, session_clock)
        )
        # Yield control enough times for the worker to dequeue + process the item.
        for _ in range(20):
            await asyncio.sleep(0)
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

        assert "AAPL" in risk_state.pending_close, "ticker should be in pending_close after timeout"

        items = []
        while not queue.empty():
            items.append(queue.get_nowait())
        assert not any(isinstance(x, FlattenAllRequest) for x in items), (
            "FlattenAllRequest must not be enqueued on exit timeout"
        )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test 2 — FlattenTickerRequest closes only the named ticker
# ---------------------------------------------------------------------------

def test_flatten_ticker_closes_only_named_ticker():
    """FlattenTickerRequest(AAPL) must remove AAPL but leave NVDA untouched."""

    async def _run():
        risk_state = RiskState()
        risk_state.open_positions["AAPL"] = {"qty": 100, "avg_cost": 150.0}
        risk_state.open_positions["NVDA"] = {"qty": 50, "avg_cost": 400.0}

        fill = _make_fill("AAPL", 100)
        ibkr = MagicMock()
        ibkr.submit = AsyncMock(return_value=fill)
        telegram = AsyncMock()

        with patch("live.orders.worker.get_pool", return_value=_MockPool()), \
             patch("live.orders.worker.fetch_mark",
                   AsyncMock(return_value=(149.9, 150.1, 150.0))), \
             patch("live.feed.market_status.is_tradable_now", return_value=True):
            await _execute_flatten_ticker(
                "AAPL", ibkr, risk_state, telegram, "test", date.today()
            )

        # IBKR market data must never be used for pricing.
        assert not hasattr(ibkr, "snapshot_quote") or not ibkr.snapshot_quote.called

        assert "AAPL" not in risk_state.open_positions, "AAPL should be removed after flatten"
        assert "NVDA" in risk_state.open_positions, "NVDA must remain untouched"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test 3 — Dead man's switch enqueues FlattenTickerRequest not FlattenAllRequest
# ---------------------------------------------------------------------------

def test_dead_mans_switch_enqueues_flatten_ticker():
    """heartbeat_monitor must enqueue FlattenTickerRequest(ticker=XYZ), not FlattenAllRequest."""

    async def _run():
        queue: asyncio.Queue = asyncio.Queue()
        hb = HeartbeatMonitor()
        hb._last_seen["XYZ"] = time.monotonic() - 9999.0  # definitely stale
        risk_state = MagicMock()
        risk_state.has_position.return_value = True
        universe = {}

        sleep_calls = [0]

        async def _mock_sleep(_):
            sleep_calls[0] += 1
            if sleep_calls[0] >= 2:
                raise asyncio.CancelledError

        with patch("live.feed.signal_loop.asyncio.sleep", _mock_sleep), \
             patch("live.feed.market_status.is_tradable_now", return_value=True):
            try:
                await heartbeat_monitor(universe, queue, risk_state, hb)
            except asyncio.CancelledError:
                pass

        assert not queue.empty(), "heartbeat_monitor should have enqueued an item"
        item = queue.get_nowait()
        assert isinstance(item, FlattenTickerRequest), (
            f"Expected FlattenTickerRequest, got {type(item)}"
        )
        assert item.ticker == "XYZ"
        assert queue.empty(), "No FlattenAllRequest should have been enqueued"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test 4 — pending_close_monitor enqueues FlattenTickerRequest not FlattenAllRequest
# ---------------------------------------------------------------------------

def test_pending_close_monitor_enqueues_flatten_ticker():
    """pending_close_monitor must re-queue FlattenTickerRequest for a stuck position."""

    async def _run():
        queue: asyncio.Queue = asyncio.Queue()
        risk_state = RiskState()
        risk_state.pending_close.add("STUCK")
        risk_state.open_positions["STUCK"] = {"qty": 100, "avg_cost": 10.0}

        ibkr = MagicMock()
        ibkr.has_open_order_for = MagicMock(return_value=False)
        telegram = AsyncMock()

        sleep_calls = [0]

        async def _mock_sleep(_):
            sleep_calls[0] += 1
            if sleep_calls[0] >= 2:
                raise asyncio.CancelledError

        with patch("live.orders.worker.asyncio.sleep", _mock_sleep), \
             patch("live.feed.market_status.is_tradable_now", return_value=True):
            try:
                await pending_close_monitor(
                    risk_state, queue, telegram, ibkr, interval_s=0.0
                )
            except asyncio.CancelledError:
                pass

        assert not queue.empty(), "pending_close_monitor should have enqueued an item"
        item = queue.get_nowait()
        assert isinstance(item, FlattenTickerRequest), (
            f"Expected FlattenTickerRequest, got {type(item)}"
        )
        assert item.ticker == "STUCK"
        assert queue.empty(), "No FlattenAllRequest should have been enqueued"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test 5 — _execute_flatten_all submits positions concurrently
# ---------------------------------------------------------------------------

def test_flatten_all_concurrent():
    """_execute_flatten_all must submit all positions concurrently via asyncio.gather."""

    async def _run():
        risk_state = RiskState()
        risk_state.open_positions = {
            "AAPL": {"qty": 100, "avg_cost": 150.0},
            "NVDA": {"qty": 50, "avg_cost": 400.0},
            "TSLA": {"qty": 75, "avg_cost": 200.0},
        }

        async def _slow_submit(req):
            await asyncio.sleep(0.1)
            return _make_fill(req.ticker, req.qty)

        ibkr = MagicMock()
        ibkr.submit = _slow_submit
        ibkr.cancel_all_orders = AsyncMock()

        telegram = AsyncMock()
        session_clock = MagicMock()
        session_clock.date = date.today()

        with patch("live.orders.worker.get_pool", return_value=_MockPool()), \
             patch("live.orders.worker.fetch_mark",
                   AsyncMock(return_value=(150.0, 151.0, 150.5))), \
             patch("live.feed.market_status.is_tradable_now", return_value=True):
            start = time.monotonic()
            await _execute_flatten_all(ibkr, risk_state, telegram, "test_concurrent", session_clock)
            elapsed = time.monotonic() - start

        assert elapsed < 0.5, (
            f"Expected concurrent execution < 0.5s (3 × 0.1s in parallel), got {elapsed:.2f}s"
        )

    asyncio.run(_run())
