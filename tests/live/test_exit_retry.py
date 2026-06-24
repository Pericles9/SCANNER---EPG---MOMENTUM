"""Exit-retry hardening tests (YYGH exit-timeout loop fix).

Covers:
  Fix A — order_worker is not a second retry authority: a duplicate exit for a ticker
          already in pending_close is skipped; the EXIT TIMEOUT alert fires once; an
          exit timeout does not re-arm the strategy (no on_fill_failed call).
  Fix B — unfillable exits escalate to a MARKET order during RTH (limit-only in extended
          hours), park for manual review at a hard cap, and pending_close auto-reconciles
          a phantom position against IBKR.

Offline; no Polygon / IBKR / DB required.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from live.orders.risk import OrderRequest, RiskState
from live.orders.worker import (
    _MANUAL_REVIEW_FAILS,
    _MARKET_ESCALATION_FAILS,
    _build_flatten_request,
    _execute_flatten_ticker,
    order_worker,
    pending_close_monitor,
)


def _drive_worker(queue, risk_state, ibkr, telegram):
    """Run order_worker just long enough to process queued items, then cancel."""
    async def _run():
        session_clock = MagicMock()
        session_clock.date = date.today()
        task = asyncio.create_task(order_worker(queue, risk_state, ibkr, telegram, session_clock))
        for _ in range(30):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(_run())


def _exit_req(ticker="YYGH", on_fill_failed=None):
    return OrderRequest(
        ticker=ticker, side="SELL", qty=0, session_bucket="regular_hours",
        is_entry=False, exit_reason="VWAP_CROSS", limit_price=0.08,
        on_fill_failed=on_fill_failed,
    )


# ── Fix A: order_worker is not a second retry authority ───────────────────────

def test_exit_already_pending_close_is_skipped():
    """An exit for a ticker already in pending_close must NOT be re-submitted."""
    rs = RiskState()
    rs.open_positions["YYGH"] = {"qty": 5952, "avg_cost": 0.17}
    rs.pending_close.add("YYGH")               # monitor already owns the retry
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait(_exit_req())
    ibkr = MagicMock()
    ibkr.submit = AsyncMock(return_value=None)
    telegram = AsyncMock()

    _drive_worker(q, rs, ibkr, telegram)

    ibkr.submit.assert_not_awaited()           # duplicate skipped before submit


def test_exit_timeout_alerts_once_and_no_rearm():
    """First timeout → pending_close + one alert; on_fill_failed (re-arm) NOT called."""
    rs = RiskState()
    rs.open_positions["YYGH"] = {"qty": 5952, "avg_cost": 0.17}
    rearm = MagicMock()
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait(_exit_req(on_fill_failed=rearm))
    ibkr = MagicMock()
    ibkr.submit = AsyncMock(return_value=None)  # never fills
    telegram = AsyncMock()

    _drive_worker(q, rs, ibkr, telegram)

    assert "YYGH" in rs.pending_close
    rearm.assert_not_called()                   # strategy is NOT re-armed on timeout
    timeout_alerts = [c for c in telegram.send_silent.call_args_list
                      if "EXIT TIMEOUT" in c.args[0]]
    assert len(timeout_alerts) == 1


# ── Fix B: order type + escalation ────────────────────────────────────────────

def test_submit_builds_market_order():
    """ibkr.submit builds a MarketOrder when order_type='MKT' and returns a MKT Fill."""
    from ib_insync import MarketOrder
    from live.orders.ibkr import IBKRClient

    client = IBKRClient()
    trade = MagicMock()
    trade.order.orderId = 123
    trade.orderStatus.status = "Filled"
    trade.orderStatus.avgFillPrice = 0.13
    ib = MagicMock()
    ib.qualifyContractsAsync = AsyncMock(return_value=None)
    ib.placeOrder = MagicMock(return_value=trade)
    client._ib = ib

    req = OrderRequest(
        ticker="YYGH", side="SELL", qty=5952, session_bucket="regular_hours",
        is_entry=False, exit_reason="pending_close_retry", order_type="MKT",
        limit_price=None, expected_price=0.0,
    )
    fill = asyncio.run(client.submit(req))

    assert fill is not None and fill.order_type == "MKT"
    placed_order = ib.placeOrder.call_args.args[1]
    assert isinstance(placed_order, MarketOrder)


def test_build_flatten_request_market_vs_limit():
    # use_market=True → MKT, no limit price.
    mkt = asyncio.run(_build_flatten_request("YYGH", 100, 0.17, "r", "regular_hours", True))
    assert mkt.order_type == "MKT" and mkt.limit_price is None

    # use_market=False → marketable LMT priced from Massive bid - offset.
    with patch("live.orders.worker.fetch_mark", AsyncMock(return_value=(0.13, 0.14, 0.13))):
        lmt = asyncio.run(_build_flatten_request("YYGH", 100, 0.17, "r", "regular_hours", False))
    assert lmt.order_type == "LMT" and lmt.limit_price is not None and lmt.limit_price < 0.13


def _run_flatten(ticker, rs, ibkr, telegram, bkt="regular_hours"):
    with patch("live.feed.market_status.is_tradable_now", return_value=True), \
         patch("live.orders.worker._bkt_now", return_value=bkt), \
         patch("live.orders.worker.fetch_mark", AsyncMock(return_value=(0.13, 0.14, 0.13))):
        asyncio.run(_execute_flatten_ticker(ticker, ibkr, rs, telegram, "retry", date.today()))


def test_flatten_escalates_to_market_in_rth():
    rs = RiskState()
    rs.open_positions["YYGH"] = {"qty": 5952, "avg_cost": 0.17}
    rs.pending_close_failures["YYGH"] = _MARKET_ESCALATION_FAILS   # at the threshold
    ibkr = MagicMock()
    ibkr.submit = AsyncMock(return_value=None)
    _run_flatten("YYGH", rs, ibkr, AsyncMock(), bkt="regular_hours")
    assert ibkr.submit.call_args.args[0].order_type == "MKT"


def test_flatten_stays_limit_in_extended_hours():
    rs = RiskState()
    rs.open_positions["YYGH"] = {"qty": 5952, "avg_cost": 0.17}
    rs.pending_close_failures["YYGH"] = _MARKET_ESCALATION_FAILS + 3   # past threshold
    ibkr = MagicMock()
    ibkr.submit = AsyncMock(return_value=None)
    _run_flatten("YYGH", rs, ibkr, AsyncMock(), bkt="pre_market")   # extended hours
    assert ibkr.submit.call_args.args[0].order_type == "LMT"        # market rejected outside RTH


def test_flatten_parks_at_hard_cap():
    rs = RiskState()
    rs.open_positions["YYGH"] = {"qty": 5952, "avg_cost": 0.17}
    rs.pending_close_failures["YYGH"] = _MANUAL_REVIEW_FAILS - 1     # next fail hits the cap
    ibkr = MagicMock()
    ibkr.submit = AsyncMock(return_value=None)
    telegram = AsyncMock()
    _run_flatten("YYGH", rs, ibkr, telegram, bkt="regular_hours")

    assert "YYGH" in rs.manual_review_required
    assert "YYGH" not in rs.pending_close                           # parked, no more auto-retry
    park_alerts = [c for c in telegram.send_silent.call_args_list
                   if "MANUAL REVIEW" in c.args[0]]
    assert len(park_alerts) == 1


# ── Fix B: pending_close auto-reconcile against IBKR ──────────────────────────

def test_pending_close_monitor_reconciles_phantom():
    """A pending_close ticker that IBKR reports flat is reconciled out (no phantom chase)."""
    rs = RiskState()
    rs.open_positions["YYGH"] = {"qty": 5952, "avg_cost": 0.17}     # stale risk_state
    rs.pending_close.add("YYGH")
    ibkr = MagicMock()
    ibkr.get_open_positions = MagicMock(return_value={})            # flat at broker
    q: asyncio.Queue = asyncio.Queue()
    telegram = AsyncMock()

    async def _run():
        calls = [0]
        async def _sleep(_):
            calls[0] += 1
            if calls[0] >= 2:
                raise asyncio.CancelledError
        with patch("live.orders.worker.asyncio.sleep", _sleep), \
             patch("live.feed.market_status.is_tradable_now", return_value=True):
            try:
                await pending_close_monitor(rs, q, telegram, ibkr, interval_s=0.01)
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())

    assert "YYGH" not in rs.pending_close
    assert "YYGH" not in rs.open_positions
