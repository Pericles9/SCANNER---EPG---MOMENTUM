"""IBKR execution wrapper via ib_insync."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ib_insync import IB, LimitOrder, Stock, Trade

from live.config import CFG
from live.orders.risk import OrderRequest

log = logging.getLogger(__name__)


def _compute_slippage_bps(fill_price: float, expected_price: float, side: str) -> float:
    """Slippage in basis points. Positive = worse than expected."""
    if expected_price <= 0:
        return 0.0
    if side == "BUY":
        return (fill_price - expected_price) / expected_price * 10_000
    else:  # SELL
        return (expected_price - fill_price) / expected_price * 10_000


@dataclass
class Fill:
    ticker: str
    side: str
    qty: int           # original requested qty
    filled_qty: int    # actual shares filled (< qty for partial)
    remaining_qty: int # shares not filled (0 for complete fills)
    fill_price: float  # avg fill price for filled shares
    ibkr_order_id: int
    session_bucket: str
    order_type: str
    limit_price: Optional[float]
    submitted_ns: int
    filled_at: datetime
    exit_reason: Optional[str]
    is_entry: bool
    intraday_pct: float
    expected_price: float  # signal-time price estimate
    slippage_bps: float    # (fill_price - expected_price) / expected_price * 10000 for buys
    status: str            # 'filled' | 'partial_cancelled'


class IBKRClient:
    def __init__(self) -> None:
        self._ib = IB()
        self._host = os.environ.get("IBKR_HOST", "host.docker.internal")
        self._port = int(os.environ.get("IBKR_PORT", "4002"))
        self._client_id = int(os.environ.get("IBKR_CLIENT_ID", "1"))

    async def connect(self) -> None:
        await self._ib.connectAsync(self._host, self._port, clientId=self._client_id)
        log.info("IBKR connected: %s:%d clientId=%d", self._host, self._port, self._client_id)

    async def disconnect(self) -> None:
        self._ib.disconnect()

    def get_open_positions(self) -> dict[str, tuple[int, float]]:
        """Returns {ticker: (qty, avg_cost)} for all currently open IBKR positions."""
        return {
            p.contract.symbol: (int(p.position), float(p.avgCost))
            for p in self._ib.positions()
            if p.position != 0
        }

    async def get_account_equity(self) -> float:
        """Return current net liquidation value from IBKR account values."""
        vals = self._ib.accountValues()
        for v in vals:
            if v.tag == "NetLiquidation" and v.currency == "USD":
                try:
                    return float(v.value)
                except (ValueError, TypeError):
                    pass
        log.warning("IBKR: NetLiquidation not found in account values")
        return 0.0

    async def submit(self, request: OrderRequest) -> Optional[Fill]:
        """Submit order and wait for fill. Returns Fill or None on full timeout with no fill."""
        contract = Stock(request.ticker, "SMART", "USD")
        await self._ib.qualifyContractsAsync(contract)

        # All orders are limit orders active outside RTH.
        # Market orders are rejected by IBKR outside regular trading hours.
        if request.limit_price is None:
            log.error(
                "%s: order missing limit_price — cannot submit without a limit",
                request.ticker,
            )
            return None
        order = LimitOrder(
            request.side,
            request.qty,
            request.limit_price,
            tif="DAY",
            outsideRth=True,
        )
        order_type = "LMT"

        submitted_ns = time.time_ns()
        trade: Trade = self._ib.placeOrder(contract, order)
        log.info("IBKR: placed %s %s %d @ %s (id=%s)",
                 request.side, request.ticker, request.qty,
                 request.limit_price or "MKT", trade.order.orderId)

        deadline = CFG.order_execution.unfilled_cancel_sec
        elapsed = 0.0
        interval = 0.25
        while elapsed < deadline:
            await asyncio.sleep(interval)
            elapsed += interval
            if trade.orderStatus.status == "Filled":
                fill_price = trade.orderStatus.avgFillPrice
                slippage = _compute_slippage_bps(fill_price, request.expected_price, request.side)
                log.info("IBKR: filled %s %s %d @ %.4f slippage=%.1fbps",
                         request.ticker, request.side, request.qty, fill_price, slippage)
                return Fill(
                    ticker=request.ticker,
                    side=request.side,
                    qty=request.qty,
                    filled_qty=request.qty,
                    remaining_qty=0,
                    fill_price=fill_price,
                    ibkr_order_id=trade.order.orderId,
                    session_bucket=request.session_bucket,
                    order_type=order_type,
                    limit_price=request.limit_price,
                    submitted_ns=submitted_ns,
                    filled_at=datetime.now(timezone.utc),
                    exit_reason=request.exit_reason,
                    is_entry=request.is_entry,
                    intraday_pct=request.intraday_pct,
                    expected_price=request.expected_price,
                    slippage_bps=slippage,
                    status="filled",
                )

        # Timeout — cancel and check for partial fill
        log.warning("%s: unfilled after %.1fs — cancelling", request.ticker, deadline)
        self._ib.cancelOrder(trade.order)
        # Wait briefly for cancel confirm and any final fill event
        await asyncio.sleep(1.0)

        filled = int(trade.orderStatus.filled)
        remaining = int(trade.orderStatus.remaining)
        if filled > 0:
            avg_fill = trade.orderStatus.avgFillPrice
            slippage = _compute_slippage_bps(avg_fill, request.expected_price, request.side)
            log.warning("%s: partial fill %d/%d @ %.4f — status=partial_cancelled",
                        request.ticker, filled, request.qty, avg_fill)
            return Fill(
                ticker=request.ticker,
                side=request.side,
                qty=request.qty,
                filled_qty=filled,
                remaining_qty=remaining,
                fill_price=avg_fill,
                ibkr_order_id=trade.order.orderId,
                session_bucket=request.session_bucket,
                order_type=order_type,
                limit_price=request.limit_price,
                submitted_ns=submitted_ns,
                filled_at=datetime.now(timezone.utc),
                exit_reason=request.exit_reason,
                is_entry=request.is_entry,
                intraday_pct=request.intraday_pct,
                expected_price=request.expected_price,
                slippage_bps=slippage,
                status="partial_cancelled",
            )

        return None

    def is_connected(self) -> bool:
        return self._ib.isConnected()

    async def account_values(self) -> list:
        return self._ib.accountValues()

    async def snapshot_quote(self, ticker: str) -> tuple[float, float]:
        """Return (bid, ask) snapshot from IBKR — execution source of truth.

        Used by startup_position_triage to validate that a recovered position
        is still tradable (price != (0, 0)). Returns (0.0, 0.0) on any failure
        — caller treats that as "halted/suspended" and launches a watcher.
        """
        try:
            contract = Stock(ticker, "SMART", "USD")
            await self._ib.qualifyContractsAsync(contract)
            tickers = await self._ib.reqTickersAsync(contract)
            if not tickers:
                return (0.0, 0.0)
            t = tickers[0]
            bid = float(t.bid) if t.bid is not None and t.bid > 0 else 0.0
            ask = float(t.ask) if t.ask is not None and t.ask > 0 else 0.0
            return (bid, ask)
        except Exception:
            log.exception("IBKR snapshot_quote failed for %s", ticker)
            return (0.0, 0.0)

    def has_open_order_for(self, ticker: str) -> bool:
        """Return True if IBKR has a live (unfilled, uncancelled) order for this ticker.

        Uses openTrades() which only returns our client's active trades. Called by
        pending_close_monitor before retrying a flatten so we don't pile additional
        sell orders onto a position that already has one working.
        """
        try:
            for trade in self._ib.openTrades():
                if trade.contract.symbol == ticker:
                    status = trade.orderStatus.status
                    if status not in ("Filled", "Cancelled", "Inactive"):
                        return True
        except Exception:
            log.exception("has_open_order_for(%s): error querying openTrades", ticker)
        return False

    async def cancel_all_orders(self) -> None:
        open_orders = self._ib.openOrders()
        for order in open_orders:
            self._ib.cancelOrder(order)
            log.info("IBKR: cancelled order %s", order.orderId)
