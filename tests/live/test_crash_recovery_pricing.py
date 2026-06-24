"""Task 2/3 — crash-recovery pricing comes from Massive, never IBKR market data.

Exercises `_close_with_limits` directly (extended-hours flatten path):
  • prices the limit from Massive NBBO and submits to IBKR; IBKR market data
    (reqMktData / reqTickersAsync) is never touched;
  • when Massive returns no price, recovery does NOT blind-fire an order and does
    NOT mark STUCK — it defers (manual review, non-halting) so the main loop can
    start;
  • a guard asserting the recovery module exposes no IBKR market-data call.

Offline; no Polygon / IBKR / DB required.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import live.recovery.crash_recovery as cr
from live.recovery.crash_recovery import _CloseRecord, _close_with_limits


def _filled_trade(price: float):
    tr = MagicMock()
    tr.orderStatus.status = "Filled"
    tr.orderStatus.avgFillPrice = price
    return tr


def _rec():
    return _CloseRecord(ticker="CAST", ibkr_qty=272, strategy_id="scanner_vwap")


def test_prices_from_massive_and_submits_to_ibkr():
    rec = _rec()
    ib = MagicMock()
    ib.placeOrder.return_value = _filled_trade(3.60)
    telegram = AsyncMock()

    with patch("live.recovery.crash_recovery.fetch_mark",
               AsyncMock(return_value=(3.65, 3.70, 3.66))):
        asyncio.run(_close_with_limits(
            ib, MagicMock(), "SELL", 272, True, 3.685, rec, "CAST", telegram, "key",
        ))

    assert rec.final_status == "CLOSED"
    assert ib.placeOrder.called                      # order submitted to IBKR
    # IBKR market data must never be used for pricing.
    assert not ib.reqMktData.called
    assert not ib.reqTickersAsync.called


def test_massive_empty_defers_without_blind_order():
    """No Massive price → do not submit, do not STUCK; DEFER (non-halting) + alert."""
    rec = _rec()
    ib = MagicMock()
    telegram = AsyncMock()

    with patch("live.recovery.crash_recovery.fetch_mark",
               AsyncMock(return_value=(None, None, None))), \
         patch("live.recovery.crash_recovery._MASSIVE_PRICE_RETRY_S", 0.0):
        asyncio.run(_close_with_limits(
            ib, MagicMock(), "SELL", 272, True, 3.685, rec, "CAST", telegram, "key",
        ))

    assert rec.final_status == "DEFERRED"            # routed to the non-halting bucket
    assert not ib.placeOrder.called                  # no blind avg_cost order
    assert not ib.reqMktData.called
    telegram.send_silent.assert_awaited()            # manual-review alert fired


def test_paper_account_no_ibkr_data_no_longer_blocks():
    """The old 10089 stall is gone: with a Massive price, recovery proceeds even
    though IBKR market data would return nothing (it is never queried)."""
    rec = _rec()
    ib = MagicMock()
    ib.placeOrder.return_value = _filled_trade(3.55)

    with patch("live.recovery.crash_recovery.fetch_mark",
               AsyncMock(return_value=(3.60, None, 3.62))):
        asyncio.run(_close_with_limits(
            ib, MagicMock(), "SELL", 272, True, 3.685, rec, "CAST", AsyncMock(), "key",
        ))

    assert rec.final_status == "CLOSED"
    assert not ib.reqMktData.called


def test_recovery_module_has_no_ibkr_marketdata_helper():
    """Guard: the recovery module must not define/import an IBKR market-data getter
    (the former _get_quote/reqMktData path). Pricing goes through _get_quote_massive."""
    assert not hasattr(cr, "_get_quote")             # IBKR reqMktData helper removed
    assert hasattr(cr, "_get_quote_massive")         # Massive REST replacement present
