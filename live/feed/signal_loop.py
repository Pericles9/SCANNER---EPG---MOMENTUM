"""Per-ticker signal loop (Process 2) + dead man's switch heartbeat monitor."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from live.session_clock import SessionClock

from live.config import CFG
from live.feed.context import TickerContext
from live.orders.risk import FlattenAllRequest, FlattenTickerRequest, OrderRequest, RiskState
from live.signals.live_state import SignalResult

log = logging.getLogger(__name__)

_HEARTBEAT_CHECK_INTERVAL_S = 5.0
_QUEUE_SIZE = 1000


def _is_trade(msg: dict) -> bool:
    return msg.get("ev") == "T"


async def signal_loop(
    ctx: TickerContext,
    order_queue: asyncio.Queue,
    risk_state: RiskState,
    hot_ticks: list,
    hot_quotes: list,
    hot_signal_events: list,
    hot_hawkes_refits: list,
    heartbeat: "HeartbeatMonitor",
    session_clock: "SessionClock",
    disqualify_callback=None,
    session_done_callback=None,
) -> None:
    """Per-ticker asyncio task. Blocks until context fetch completes."""
    await ctx.state_ready.wait()

    while True:
        try:
            raw_msg = await ctx.queue.get()
        except asyncio.CancelledError:
            raise

        ticker = ctx.ticker
        session_date_val = session_clock.date

        bkt = _infer_session_bucket(raw_msg.get("t", 0))

        if _is_trade(raw_msg):
            result: SignalResult = ctx.signal_state.update_trade(raw_msg)

            hot_ticks.append((
                ticker, session_date_val,
                result.sip_timestamp,
                raw_msg.get("p", 0.0),
                raw_msg.get("s", 0),
                result.side,
                bkt,
            ))

            for ev in result.signal_events:
                hot_signal_events.append(ev)

            if result.hawkes_refit_record is not None:
                hot_hawkes_refits.append(result.hawkes_refit_record)

        else:
            result = ctx.signal_state.update_quote(raw_msg)

            hot_quotes.append((
                ticker, session_date_val,
                result.sip_timestamp,
                raw_msg.get("bp"),
                raw_msg.get("ap"),
                raw_msg.get("bs"),
                raw_msg.get("as"),
                bkt,
            ))

        if result.disqualify:
            if result.session_done:
                log.info("%s: strategy session complete — removing from universe", ticker)
                if session_done_callback is not None:
                    await session_done_callback()
            else:
                log.info("%s: SF disqualified — removing from universe", ticker)
                if disqualify_callback is not None:
                    await disqualify_callback()
            return

        if result.order_signal:
            price = raw_msg.get("p", 0.0)
            req = _build_order_request(
                ctx, result.order_signal, price, bkt, raw_msg, risk_state
            )
            if req is not None:
                if result.order_signal != "ENTRY":
                    # Attach fill callbacks so order_worker can confirm exit state.
                    # record_exit() flips _in_position=False only after broker confirms.
                    # clear_exit_pending() re-arms exit signals if the order fails.
                    req.on_fill_confirmed = ctx.signal_state.record_exit
                    req.on_fill_failed = ctx.signal_state.clear_exit_pending
                order_queue.put_nowait(req)
                if result.order_signal == "ENTRY":
                    ctx.signal_state.record_entry(bkt, ctx.signal_state.current_imbalance())
                else:
                    # Mark exit in-flight: keeps _in_position=True, suppresses duplicate signals.
                    ctx.signal_state.signal_exit()

        heartbeat.update(ticker)


def _infer_session_bucket(ts_ns: int) -> str:
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    dt = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).astimezone(ZoneInfo("America/New_York"))
    et_sec = dt.hour * 3600 + dt.minute * 60 + dt.second
    if et_sec < 9 * 3600 + 30 * 60:
        return "pre_market"
    if et_sec < 16 * 3600:
        return "regular_hours"
    return "post_market"


def _build_order_request(
    ctx: TickerContext,
    signal: str,
    price: float,
    bkt: str,
    raw_msg: dict,
    risk_state: RiskState,
) -> Optional[OrderRequest]:
    ticker = ctx.ticker

    if signal == "ENTRY":
        qty = risk_state.compute_position_size(price, bkt)
        ask = raw_msg.get("ap") or (price + CFG.order_execution.pre_market_limit_offset)
        limit_price = round(ask + CFG.order_execution.pre_market_limit_offset, 2)
        return OrderRequest(
            ticker=ticker,
            side="BUY",
            qty=qty,
            session_bucket=bkt,
            is_entry=True,
            limit_price=limit_price,
            intraday_pct=ctx.signal_state.intraday_pct,
            expected_price=limit_price,
        )

    if signal in ("EXIT_D", "LULD", "EPG_CLOSE", "VWAP_CLOSE", "HARD_STOP"):
        bid = (
            ctx.signal_state.last_bid
            if ctx.signal_state.last_bid is not None
            else price - CFG.order_execution.extended_exit_offset
        )
        limit_price = round(bid - CFG.order_execution.extended_exit_offset, 2)
        return OrderRequest(
            ticker=ticker,
            side="SELL",
            qty=0,   # sentinel: order_worker fills from risk_state
            session_bucket=bkt,
            is_entry=False,
            exit_reason=signal,
            limit_price=limit_price,
            intraday_pct=ctx.signal_state.intraday_pct,
            expected_price=limit_price,
        )

    return None


class HeartbeatMonitor:
    """Tracks last-seen time per ticker for dead man's switch."""

    def __init__(self) -> None:
        self._last_seen: dict[str, float] = {}

    def update(self, ticker: str) -> None:
        self._last_seen[ticker] = time.monotonic()

    def remove(self, ticker: str) -> None:
        self._last_seen.pop(ticker, None)

    def stale_tickers(self, timeout_s: float) -> list[str]:
        now = time.monotonic()
        return [t for t, ts in self._last_seen.items() if now - ts > timeout_s]


async def heartbeat_monitor(
    universe: dict,
    order_queue: asyncio.Queue,
    risk_state: RiskState,
    heartbeat: HeartbeatMonitor,
) -> None:
    """Periodic dead man's switch check."""
    from live.feed import market_status
    while True:
        await asyncio.sleep(_HEARTBEAT_CHECK_INTERVAL_S)
        # The dead man's switch guards against a frozen feed during the session.
        # When the market is closed there is legitimately no feed, so a stale
        # heartbeat is expected — do not flatten (it can't fill and only spams).
        if not market_status.is_tradable_now():
            continue
        stale = heartbeat.stale_tickers(CFG.risk.dead_man_timeout_s)
        for ticker in stale:
            if risk_state.has_position(ticker):
                log.critical(
                    "DEAD MAN'S SWITCH: no heartbeat for %s in %ds with open position — flattening ticker only",
                    ticker, CFG.risk.dead_man_timeout_s,
                )
                order_queue.put_nowait(
                    FlattenTickerRequest(ticker=ticker, reason="dead_mans_switch")
                )
