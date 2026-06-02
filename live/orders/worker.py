"""Process 3: single asyncio consumer for the order_queue."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from live.config import CFG
from live.db.pool import get_pool
from live.orders.ibkr import Fill, IBKRClient
from live.orders.risk import FlattenAllRequest, OrderRequest, RiskState

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_ALERT_START_HOUR = 4   # 04:00 ET inclusive
_ALERT_END_HOUR = 20    # 20:00 ET exclusive

_last_wake_t: list[float] = [0.0]


def get_last_wake_t() -> float:
    """Return monotonic time of the last order_worker loop iteration."""
    return _last_wake_t[0]


async def order_worker(
    order_queue: asyncio.Queue,
    risk_state: RiskState,
    ibkr: IBKRClient,
    telegram,
    session_date: date,
) -> None:
    """Consume order_queue. Single writer to RiskState."""
    while True:
        _last_wake_t[0] = time.monotonic()
        request = await order_queue.get()

        if isinstance(request, FlattenAllRequest):
            await _execute_flatten_all(ibkr, risk_state, telegram, request.reason)
            continue

        if not isinstance(request, OrderRequest):
            log.warning("order_worker: unknown request type %s", type(request))
            continue

        if not risk_state.allows(request):
            log.warning("Risk check blocked: %s %s", request.side, request.ticker)
            if risk_state._loss_limit_hit:
                await telegram.send_silent(
                    f"Daily loss limit hit — entries blocked. PnL: {risk_state.daily_pnl:.2f}"
                )
                # Auto-kill: flatten all open positions once when loss limit is hit
                if CFG.risk.auto_kill_on_daily_loss and not risk_state._auto_kill_fired:
                    risk_state._auto_kill_fired = True
                    log.critical(
                        "Auto-kill: daily loss limit hit — flattening all open positions"
                    )
                    order_queue.put_nowait(FlattenAllRequest(reason="auto_kill_daily_loss"))
            continue

        # Resolve qty for exit orders (sentinel qty=0)
        if not request.is_entry and request.qty == 0:
            pos = risk_state.open_positions.get(request.ticker)
            if pos is None:
                log.warning("Exit for %s but no open position tracked", request.ticker)
                continue
            request.qty = pos["qty"]

        fill: Optional[Fill] = await ibkr.submit(request)
        if fill is None:
            if not request.is_entry:
                log.critical(
                    "Exit order timed out: %s %s — adding to pending_close, escalating to FlattenAll",
                    request.side, request.ticker,
                )
                risk_state.pending_close.add(request.ticker)
                if request.on_fill_failed is not None:
                    request.on_fill_failed()
                await telegram.send_silent(
                    f"EXIT TIMEOUT: {request.ticker} — escalating to FlattenAll"
                )
                await _execute_flatten_all(ibkr, risk_state, telegram,
                                           f"exit_timeout_{request.ticker}")
            else:
                log.warning("Entry order timed out: %s %s — skipping",
                            request.side, request.ticker)
            continue

        # Write fill records in explicit transaction (non-negotiable)
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                order_id = await _write_order(conn, fill, session_date)
                await _update_position(conn, fill, order_id, session_date)

        # Update risk state (only order_worker writes RiskState)
        risk_state.record_fill(
            fill.ticker, fill.side, fill.qty, fill.fill_price,
            filled_qty=fill.filled_qty,
        )
        risk_state.pending_close.discard(fill.ticker)
        if request.on_fill_confirmed is not None:
            request.on_fill_confirmed()

        await _notify_fill(telegram, fill, risk_state)

        log.info("Fill: %s %s %d/%d @ %.4f slippage=%.1fbps status=%s reason=%s",
                 fill.ticker, fill.side, fill.filled_qty, fill.qty,
                 fill.fill_price, fill.slippage_bps, fill.status, fill.exit_reason)


async def _execute_flatten_all(
    ibkr: IBKRClient,
    risk_state: RiskState,
    telegram,
    reason: str,
) -> None:
    log.critical("FLATTEN ALL: reason=%s", reason)
    await telegram.send_silent(f"CRITICAL: FLATTEN ALL triggered — {reason}")
    await ibkr.cancel_all_orders()
    await ibkr.flatten_all(risk_state)
    await asyncio.sleep(10)
    log.critical("FLATTEN ALL: complete")
    await telegram.send_silent("FLATTEN ALL: complete — all positions should be flat")


def _to_ns(dt) -> int:
    return int(dt.timestamp() * 1e9)


async def _write_order(conn, fill: Fill, session_date: date) -> int:
    notional = fill.fill_price * fill.filled_qty
    filled_ns = _to_ns(fill.filled_at)
    row = await conn.fetchrow(
        """
        INSERT INTO orders
            (strategy_id, ticker, session_date, session_bucket,
             submitted_ns, filled_ns, side, qty, order_type,
             limit_price, fill_price, notional, status,
             broker_order_id, signal_reason,
             filled_qty, remaining_qty, expected_price, slippage_bps)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
        RETURNING id
        """,
        CFG.strategy_id,
        fill.ticker,
        session_date,
        fill.session_bucket,
        fill.submitted_ns,
        filled_ns,
        fill.side,
        fill.qty,
        fill.order_type,
        fill.limit_price,
        fill.fill_price,
        notional,
        fill.status,
        str(fill.ibkr_order_id),
        fill.exit_reason,
        fill.filled_qty,
        fill.remaining_qty,
        fill.expected_price if fill.expected_price > 0 else None,
        fill.slippage_bps,
    )
    return row["id"]


async def _update_position(conn, fill: Fill, order_id: int, session_date: date) -> None:
    filled_ns = _to_ns(fill.filled_at)

    if fill.is_entry:
        # Aggregate on second+ BUY for same (strategy, ticker, session). Weighted-average
        # entry price; first-fill open_ns is preserved. The prior `ON CONFLICT DO NOTHING`
        # silently dropped follow-on fills, leaving the DB position stuck at the first
        # fill's qty while IBKR accumulated the full amount.
        await conn.execute(
            """
            INSERT INTO positions
                (strategy_id, ticker, session_date, qty, avg_entry_price, open_ns)
            VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (strategy_id, ticker, session_date) DO UPDATE
                SET qty = positions.qty + EXCLUDED.qty,
                    avg_entry_price = (
                        positions.qty * positions.avg_entry_price
                        + EXCLUDED.qty * EXCLUDED.avg_entry_price
                    ) / (positions.qty + EXCLUDED.qty)
            """,
            CFG.strategy_id, fill.ticker, session_date,
            fill.filled_qty, fill.fill_price, filled_ns,
        )
    else:
        # Read entry data before deleting the position row
        entry = await conn.fetchrow(
            """
            SELECT avg_entry_price, qty, open_ns FROM positions
            WHERE strategy_id=$1 AND ticker=$2 AND session_date=$3
            """,
            CFG.strategy_id, fill.ticker, session_date,
        )
        if entry:
            entry_price = entry["avg_entry_price"]
            qty = entry["qty"]
            open_ns = entry["open_ns"]
            pnl_dollar = (fill.fill_price - entry_price) * qty
            pnl_pct = (fill.fill_price - entry_price) / entry_price if entry_price > 0 else 0.0
            hold_sec = (filled_ns - open_ns) / 1e9 if open_ns else None

            await conn.execute(
                "DELETE FROM positions WHERE strategy_id=$1 AND ticker=$2 AND session_date=$3",
                CFG.strategy_id, fill.ticker, session_date,
            )
            await conn.execute(
                """
                INSERT INTO trades
                    (strategy_id, ticker, session_date, session_bucket,
                     exit_order_id, entry_ns, exit_ns, hold_sec,
                     entry_price, exit_price, qty,
                     pnl_pct, pnl_dollar,
                     intraday_pct_at_entry, exit_reason)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                """,
                CFG.strategy_id, fill.ticker, session_date, fill.session_bucket,
                order_id,
                open_ns, filled_ns, hold_sec,
                entry_price, fill.fill_price, qty,
                pnl_pct, pnl_dollar,
                fill.intraday_pct,
                fill.exit_reason,
            )


async def _notify_fill(telegram, fill: Fill, risk_state: RiskState) -> None:
    partial_note = f" [partial {fill.filled_qty}/{fill.qty}]" if fill.status == "partial_cancelled" else ""
    if fill.is_entry:
        msg = (
            f"ENTRY: {fill.ticker} BUY {fill.filled_qty} @ ${fill.fill_price:.2f} "
            f"({fill.session_bucket}) slip={fill.slippage_bps:.1f}bps{partial_note}"
        )
    else:
        msg = (
            f"EXIT: {fill.ticker} SELL {fill.filled_qty} @ ${fill.fill_price:.2f} "
            f"reason={fill.exit_reason} slip={fill.slippage_bps:.1f}bps{partial_note} "
            f"| daily PnL: ${risk_state.daily_pnl:.2f}"
        )
    await telegram.send_silent(msg)


async def pending_close_monitor(
    risk_state: RiskState,
    order_queue: asyncio.Queue,
    telegram,
    interval_s: float = 30.0,
) -> None:
    """Retry any position in pending_close that is still open.

    pending_close is populated when an exit order times out. This task fires every
    interval_s and re-queues a FlattenAll for each stuck ticker until IBKR confirms
    flat (at which point record_fill removes the ticker from open_positions and the
    next iteration discards it from pending_close).
    """
    from live.feed import market_status
    while True:
        await asyncio.sleep(interval_s)
        # Market-hours aware: hold pending_close tickers untouched while the
        # market is closed (no orders, no Telegram spam); retry when it reopens.
        if not market_status.is_tradable_now():
            continue
        for ticker in list(risk_state.pending_close):
            if risk_state.has_position(ticker):
                log.critical(
                    "pending_close: %s still open after %.0fs — re-queuing FlattenAll",
                    ticker, interval_s,
                )
                asyncio.create_task(
                    telegram.send_silent(f"STUCK POSITION: {ticker} — retrying FlattenAll")
                )
                order_queue.put_nowait(FlattenAllRequest(reason=f"pending_close_retry_{ticker}"))
            else:
                log.info("pending_close: %s now flat — clearing", ticker)
                risk_state.pending_close.discard(ticker)


async def hourly_pnl_alert(
    risk_state: RiskState,
    telegram,
    universe: dict,
) -> None:
    """Send hourly P&L summary during trading hours (04:00–20:00 ET). Reads RiskState — never writes."""
    while True:
        await asyncio.sleep(3600)
        now_et = datetime.now(_ET)
        if not (_ALERT_START_HOUR <= now_et.hour < _ALERT_END_HOUR):
            log.debug("hourly_pnl_alert: suppressed outside trading hours (%s ET)", now_et.strftime("%H:%M"))
            continue

        n_trades = len(risk_state._trade_history)
        sign = "+" if risk_state.daily_pnl >= 0 else ""
        lines = [f"Hourly P&L: {sign}${risk_state.daily_pnl:.2f} | trades: {n_trades}"]

        for ticker, pos in risk_state.open_positions.items():
            ctx = universe.get(ticker)
            if ctx and ctx.signal_state:
                cur_price = ctx.signal_state.last_price
                unreal = (cur_price - pos["avg_cost"]) * pos["qty"]
                u_sign = "+" if unreal >= 0 else ""
                lines.append(f"  Open: {ticker} {u_sign}${unreal:.2f} unrealised")
            else:
                lines.append(f"  Open: {ticker}")

        await telegram.send_silent("\n".join(lines))
