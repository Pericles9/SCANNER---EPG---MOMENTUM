"""Crash recovery — cancel everything, flatten everything, start clean.

A PC / process crash is functionally identical to a dead man's switch trigger:
the recovery posture is the same. Cancel all open orders, flatten all open
positions, reconcile the DB to a flat state, start fresh.

This is a deliberate decision, not a degraded fallback:

  * EPG windows are 30-120s. Restart time (Docker + IBKR + Polygon reconnect)
    almost always exceeds the live window, so there is nothing meaningful to
    resume into.
  * Resuming a position with stale Hawkes state and unknown missed ticks is more
    dangerous than taking the loss and starting clean.
  * The dead man's switch already encodes this (>30s gap during a position =
    flatten). A crash is always >30s.

This module runs ONCE at startup, after IBKR connects but before any signal
loops or scanner polling begin. It is the *single* path for startup
reconciliation — there is no separate DB-vs-IBKR mismatch halt and no triage
resume logic anywhere else.

Public interface:

    result = await run_crash_recovery(ib, pool, telegram, session_date)

The caller must inspect the result: a non-empty ``stuck_tickers`` or
``error_tickers`` means recovery could not get flat and the startup sequence
must halt. ``deferred_tickers`` (market closed) is a warning — continue, but
surface those tickers for manual review.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from ib_insync import IB, LimitOrder, MarketOrder, Stock

from live.config import CFG
from live.feed.massive import fetch_mark
from live.feed.market_status import current_session_bucket, is_tradable_now

log = logging.getLogger(__name__)

# ── Tunable timeouts (module-level so tests can monkeypatch to small values) ──
_CANCEL_CONFIRM_TIMEOUT_S = 5.0    # per-order cancel confirmation
_QUOTE_TIMEOUT_S = 5.0             # Massive REST price fetch timeout
_MASSIVE_PRICE_RETRIES = 2         # bounded — recovery must never wedge liveness
_MASSIVE_PRICE_RETRY_S = 0.5
_FILL_POLL_S = 1.0                 # poll cadence while waiting on a fill
_PRIMARY_TIMEOUT_S = 30.0          # first close attempt
_SECONDARY_TIMEOUT_S = 30.0        # re-priced close attempt
_FINAL_TIMEOUT_S = 60.0            # final widened attempt (extended hours)

# Extended-hours limit aggression ladder (how far we cross the spread).
_AGGR_PRIMARY = 0.01
_AGGR_SECONDARY = 0.05
_AGGR_FINAL = 0.10
_QUOTE_PROXY_WIDEN = 0.02          # widen last-trade proxy when no live quote

_DONE_ORDER_STATES = ("Filled", "Cancelled", "Inactive")
_CANCELLED_STATES = ("Cancelled", "Inactive")


@dataclass
class CrashRecoveryResult:
    had_open_positions: bool = False
    cancelled_orders: list = field(default_factory=list)   # broker_order_ids
    closed_tickers: list = field(default_factory=list)
    deferred_tickers: list = field(default_factory=list)   # outside hours — manual review
    stuck_tickers: list = field(default_factory=list)      # submitted but never filled
    error_tickers: list = field(default_factory=list)      # exception during close
    recovery_duration_ms: float = 0.0


@dataclass
class _CloseRecord:
    """Per-position outcome — drives the signal_events audit row."""
    ticker: str
    ibkr_qty: int
    strategy_id: str
    order_type: Optional[str] = None
    session_bucket: Optional[str] = None
    fill_price: Optional[float] = None
    fill_ns: Optional[int] = None
    reprice_count: int = 0
    final_status: str = "PENDING"   # CLOSED | DEFERRED | STUCK | ERROR


# ── Public entrypoint ─────────────────────────────────────────────────────────

async def run_crash_recovery(
    ib: IB,
    pool,
    telegram=None,
    session_date: Optional[date] = None,
    polygon_api_key: Optional[str] = None,
) -> CrashRecoveryResult:
    """Cancel all orders, flatten all positions, reconcile the DB to flat.

    ``ib``      — raw ib_insync IB handle (IBKRClient.ib).
    ``pool``    — asyncpg pool (connections acquired per DB step).
    ``telegram``— optional alerter (``send_silent``); None disables alerts.
    ``session_date`` — date stamped on audit rows; defaults to today.

    Normal startup (nothing open at IBKR, DB already flat) completes in well
    under 2s with no side effects.
    """
    t0 = time.monotonic()
    if session_date is None:
        session_date = date.today()
    result = CrashRecoveryResult()

    # Step 1 — cancel every open order at IBKR (go to the broker, not the DB).
    result.cancelled_orders = await _cancel_all_orders(ib)

    # Step 2 — fetch live IBKR positions. This is the ground truth.
    positions = await _fetch_positions(ib)
    result.had_open_positions = bool(positions)

    # Tickers this strategy currently owns per DB — used to attribute audit rows
    # and to detect IBKR orphans (positions with no matching DB row).
    owned = await _load_db_open_tickers(pool)

    # Step 3 — flatten every live position (per-position error isolation).
    tradable = is_tradable_now()
    records: list[_CloseRecord] = []
    for pos in positions:
        ticker = pos["ticker"]
        strat = CFG.strategy_id if ticker in owned else "UNKNOWN"
        rec = _CloseRecord(ticker=ticker, ibkr_qty=pos["qty"], strategy_id=strat)
        try:
            await _close_one(ib, pos, rec, tradable, telegram, polygon_api_key)
        except Exception:
            log.critical("Crash recovery: unhandled error closing %s", ticker, exc_info=True)
            rec.final_status = "ERROR"
        records.append(rec)
        _route_record(result, rec)

    # Step 4 + 5 — reconcile DB to flat and write audit rows (one transaction).
    await _reconcile_db_and_audit(pool, session_date, records)

    result.recovery_duration_ms = (time.monotonic() - t0) * 1000.0

    # Step 6 — single summary alert.
    await _send_summary(telegram, result, tradable)

    log.info(
        "Crash recovery complete in %.0fms: cancelled=%d closed=%s deferred=%s stuck=%s errors=%s",
        result.recovery_duration_ms, len(result.cancelled_orders),
        result.closed_tickers, result.deferred_tickers,
        result.stuck_tickers, result.error_tickers,
    )
    return result


def _route_record(result: CrashRecoveryResult, rec: _CloseRecord) -> None:
    if rec.final_status == "CLOSED":
        result.closed_tickers.append(rec.ticker)
    elif rec.final_status == "DEFERRED":
        result.deferred_tickers.append(rec.ticker)
    elif rec.final_status == "STUCK":
        result.stuck_tickers.append(rec.ticker)
    elif rec.final_status == "ERROR":
        result.error_tickers.append(rec.ticker)


# ── Step 1: cancel all open orders ────────────────────────────────────────────

async def _cancel_all_orders(ib: IB) -> list[str]:
    """Cancel every open order, confirming each individually. reqGlobalCancel is
    only a backstop because it is not awaitable and gives no per-order confirm."""
    cancelled: list[str] = []
    try:
        trades = await ib.reqAllOpenOrdersAsync()
    except Exception:
        log.exception("Crash recovery: reqAllOpenOrders failed")
        trades = []

    for tr in trades or []:
        oid = _order_id(tr)
        status = tr.orderStatus.status
        if status in _DONE_ORDER_STATES:
            log.info("Crash recovery: order %s already %s — skipping", oid, status)
            continue
        sym = getattr(tr.contract, "symbol", "?")
        log.info(
            "Crash recovery: cancelling order id=%s %s %s %s %s",
            oid, sym, tr.order.action, tr.order.totalQuantity, tr.order.orderType,
        )
        try:
            ib.cancelOrder(tr.order)
        except Exception:
            log.exception("Crash recovery: cancelOrder raised for id=%s", oid)
        if await _await_cancel(tr):
            cancelled.append(str(oid))
        else:
            log.critical(
                "Crash recovery: order id=%s (%s) did NOT confirm cancelled within %.0fs",
                oid, sym, _CANCEL_CONFIRM_TIMEOUT_S,
            )

    # Backstop only — fire-and-forget account-wide cancel.
    try:
        ib.reqGlobalCancel()
    except Exception:
        log.exception("Crash recovery: reqGlobalCancel backstop failed")

    return cancelled


async def _await_cancel(trade) -> bool:
    elapsed = 0.0
    poll = min(0.25, _CANCEL_CONFIRM_TIMEOUT_S) or 0.25
    while True:
        if trade.orderStatus.status in _CANCELLED_STATES:
            return True
        if elapsed >= _CANCEL_CONFIRM_TIMEOUT_S:
            return False
        await asyncio.sleep(poll)
        elapsed += poll


# ── Step 2: fetch live positions ──────────────────────────────────────────────

async def _fetch_positions(ib: IB) -> list[dict]:
    try:
        raw = await ib.reqPositionsAsync()
    except Exception:
        log.exception("Crash recovery: reqPositions failed")
        return []
    out: list[dict] = []
    for p in raw or []:
        contract = p.contract
        if getattr(contract, "secType", None) != "STK":
            continue
        qty = int(p.position)
        if qty == 0:
            continue
        out.append({
            "ticker": contract.symbol,
            "qty": qty,
            "avg_cost": float(getattr(p, "avgCost", 0.0) or 0.0),
        })
    return out


# ── Step 3: flatten one position ──────────────────────────────────────────────

async def _close_one(
    ib: IB, pos: dict, rec: _CloseRecord, tradable: bool, telegram,
    api_key: Optional[str] = None,
) -> None:
    ticker = pos["ticker"]
    qty = pos["qty"]

    # Outside trading hours — do not submit. Defer for manual review.
    if not tradable:
        rec.final_status = "DEFERRED"
        rec.session_bucket = "outside_hours"
        log.warning(
            "Crash recovery: %s qty=%d — outside trading hours, deferring (manual review)",
            ticker, qty,
        )
        if telegram is not None:
            await telegram.send_silent(
                f"🔴 CRASH RECOVERY: {ticker} ({qty:+d}) — outside trading hours, "
                f"cannot flatten. MANUAL REVIEW NEEDED."
            )
        return

    bkt = current_session_bucket()
    rec.session_bucket = bkt

    contract = Stock(ticker, "SMART", "USD")
    await ib.qualifyContractsAsync(contract)

    is_long = qty > 0
    action = "SELL" if is_long else "BUY"
    abs_qty = abs(qty)

    if bkt == "regular_hours":
        await _close_with_market(ib, contract, action, abs_qty, rec, ticker, telegram)
    else:
        await _close_with_limits(
            ib, contract, action, abs_qty, is_long, pos["avg_cost"], rec, ticker, telegram, api_key,
        )


async def _close_with_market(
    ib: IB, contract, action: str, qty: int, rec: _CloseRecord, ticker: str, telegram,
) -> None:
    """Regular-hours close: market order, tif=DAY. Re-submit (escalating windows)
    if it somehow does not fill, then accept STUCK."""
    rec.order_type = "MKT"
    windows = (_PRIMARY_TIMEOUT_S, _SECONDARY_TIMEOUT_S, _FINAL_TIMEOUT_S)
    for i, win in enumerate(windows):
        if i > 0:
            rec.reprice_count = i
            log.warning("Crash recovery: %s market order did not fill — resubmit #%d", ticker, i)
        order = MarketOrder(action, qty)
        order.tif = "DAY"
        trade = ib.placeOrder(contract, order)
        if await _await_fill(trade, win):
            _mark_filled(rec, trade)
            return
        _safe_cancel(ib, trade)
    await _mark_stuck(rec, ticker, telegram)


async def _close_with_limits(
    ib: IB, contract, action: str, qty: int, is_long: bool,
    avg_cost: float, rec: _CloseRecord, ticker: str, telegram,
    api_key: Optional[str] = None,
) -> None:
    """Extended-hours close: marketable limit ladder, tif=EXT, outsideRth.

    Price reference comes from Massive REST (NBBO bid/ask, last-trade fallback) —
    IBKR is execution-only and its market data is never used for pricing. Cross the
    spread aggressively, widen on each failed attempt, then STUCK.

    If Massive has no price after a bounded budget, do NOT blind-fire an avg_cost
    order into an illiquid post-market book (it cannot fill → wedges startup):
    leave the position open for manual handling (DEFERRED, non-halting) so recovery
    finishes and the main loop can start.
    """
    rec.order_type = "LMT"
    bid, ask, last = await _get_quote_massive(ticker, api_key)
    if not (bid > 0 or ask > 0 or last > 0):
        rec.final_status = "DEFERRED"
        log.critical(
            "Crash recovery: %s — no Massive price after %d attempts; leaving OPEN for manual handling",
            ticker, _MASSIVE_PRICE_RETRIES,
        )
        if telegram is not None:
            await telegram.send_silent(
                f"🔴 CRASH RECOVERY: {ticker} ({rec.ibkr_qty:+d}) — no Massive price available; "
                f"position left OPEN for manual handling. MANUAL REVIEW NEEDED."
            )
        return

    ladder = (
        (_AGGR_PRIMARY, _PRIMARY_TIMEOUT_S),
        (_AGGR_SECONDARY, _SECONDARY_TIMEOUT_S),
        (_AGGR_FINAL, _FINAL_TIMEOUT_S),
    )
    for i, (aggr, win) in enumerate(ladder):
        price = _ext_limit_price(is_long, bid, ask, last, avg_cost, aggr)
        if i > 0:
            rec.reprice_count = i
            log.warning(
                "Crash recovery: %s reprice #%d → %s limit %.2f (aggr=%.2f)",
                ticker, i, action, price, aggr,
            )
        order = LimitOrder(action, qty, price, tif="EXT", outsideRth=True)
        trade = ib.placeOrder(contract, order)
        if await _await_fill(trade, win):
            _mark_filled(rec, trade)
            return
        _safe_cancel(ib, trade)
        bid, ask, last = await _get_quote_massive(ticker, api_key)  # refresh before widening

    await _mark_stuck(rec, ticker, telegram)


def _ext_limit_price(
    is_long: bool, bid: float, ask: float, last: float, avg_cost: float, aggr: float,
) -> float:
    """Aggressive marketable limit. Long closes (SELL) cross down through the bid;
    short closes (BUY) cross up through the ask. Falls back to last-trade proxy,
    then avg_cost, when no live quote is available."""
    if is_long:
        ref = bid if bid > 0 else (
            last - _QUOTE_PROXY_WIDEN if last > 0 else avg_cost
        )
        return round(max(0.01, ref - aggr), 2)
    ref = ask if ask > 0 else (
        last + _QUOTE_PROXY_WIDEN if last > 0 else avg_cost
    )
    return round(max(0.01, ref + aggr), 2)


# ── Quote + fill helpers ──────────────────────────────────────────────────────

async def _get_quote_massive(ticker: str, api_key: Optional[str]) -> tuple[float, float, float]:
    """Return (bid, ask, last) from Massive REST. IBKR is execution-only — its market
    data is never used for pricing. Bounded retry budget so recovery never wedges the
    health heartbeat; returns (0.0, 0.0, 0.0) when Massive has nothing."""
    for attempt in range(_MASSIVE_PRICE_RETRIES):
        bid, ask, last = await fetch_mark(ticker, api_key, timeout_s=_QUOTE_TIMEOUT_S)
        if (bid and bid > 0) or (ask and ask > 0) or (last and last > 0):
            return (bid or 0.0, ask or 0.0, last or 0.0)
        if attempt + 1 < _MASSIVE_PRICE_RETRIES:
            await asyncio.sleep(_MASSIVE_PRICE_RETRY_S)
    log.warning("Crash recovery: %s — Massive returned no price after %d attempts",
                ticker, _MASSIVE_PRICE_RETRIES)
    return (0.0, 0.0, 0.0)


def _valid_px(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(v) or v <= 0:
        return 0.0
    return v


async def _await_fill(trade, timeout: float) -> bool:
    """Poll a trade until Filled or timeout. Checks before sleeping so a trade
    that is already filled returns immediately."""
    elapsed = 0.0
    poll = min(_FILL_POLL_S, timeout) if timeout > 0 else _FILL_POLL_S
    poll = poll or _FILL_POLL_S
    while True:
        if trade.orderStatus.status == "Filled":
            return True
        if elapsed >= timeout:
            return False
        await asyncio.sleep(poll)
        elapsed += poll


def _safe_cancel(ib: IB, trade) -> None:
    try:
        ib.cancelOrder(trade.order)
    except Exception:
        log.exception("Crash recovery: cancel before reprice failed")


def _mark_filled(rec: _CloseRecord, trade) -> None:
    rec.final_status = "CLOSED"
    rec.fill_price = _valid_px(getattr(trade.orderStatus, "avgFillPrice", None)) or None
    rec.fill_ns = time.time_ns()


async def _mark_stuck(rec: _CloseRecord, ticker: str, telegram) -> None:
    rec.final_status = "STUCK"
    log.critical(
        "Crash recovery: %s STUCK — could not flatten %+d after full attempt ladder",
        ticker, rec.ibkr_qty,
    )
    if telegram is not None:
        await telegram.send_silent(
            f"🔴 CRASH RECOVERY: {ticker} STUCK — could not flatten {rec.ibkr_qty:+d} "
            f"(order_type={rec.order_type}, reprices={rec.reprice_count}). MANUAL REVIEW NEEDED."
        )


def _order_id(trade) -> object:
    oid = getattr(trade.order, "orderId", None)
    if not oid:
        oid = getattr(trade.order, "permId", None)
    return oid if oid else "?"


# ── Step 4 + 5: DB reconcile + audit ──────────────────────────────────────────

async def _load_db_open_tickers(pool) -> set[str]:
    """Tickers this strategy currently has a non-flat position for (any session)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT ticker FROM positions WHERE strategy_id=$1 AND qty != 0",
            CFG.strategy_id,
        )
    return {r["ticker"] for r in rows}


async def _reconcile_db_and_audit(pool, session_date: date, records: list[_CloseRecord]) -> None:
    """Force the DB to a flat state and write one audit row per affected ticker.

    Positions for this strategy are zeroed and dangling PENDING orders are marked
    CANCELLED — all inside one explicit transaction. On clean starts (nothing to
    do) we skip the transaction entirely so there are no side effects.

    Note: DB cleanup is unconditional per spec — even DEFERRED/STUCK positions
    (still held at IBKR) have their DB rows zeroed. The system disclaims them and
    relies on the manual-review alert; on the next restart IBKR (the ground
    truth) re-surfaces them and recovery flattens them, so this stays idempotent.
    """
    strat = CFG.strategy_id
    async with pool.acquire() as conn:
        open_rows = await conn.fetchval(
            "SELECT count(*) FROM positions WHERE strategy_id=$1 AND qty != 0", strat
        )
        pending_orders = await conn.fetchval(
            "SELECT count(*) FROM orders WHERE strategy_id=$1 AND status='PENDING'", strat
        )

        if not open_rows and not pending_orders and not records:
            log.info("Crash recovery: DB already flat, no orders/positions to reconcile")
            return

        try:
            async with conn.transaction():
                if open_rows:
                    await conn.execute(
                        "UPDATE positions SET qty=0 WHERE strategy_id=$1 AND qty != 0",
                        strat,
                    )
                if pending_orders:
                    await conn.execute(
                        "UPDATE orders SET status='CANCELLED', cancel_reason='CRASH_RECOVERY' "
                        "WHERE strategy_id=$1 AND status='PENDING'",
                        strat,
                    )
                for rec in records:
                    notes = json.dumps({
                        "ibkr_qty": rec.ibkr_qty,
                        "fill_price": rec.fill_price,
                        "fill_ns": rec.fill_ns,
                        "order_type": rec.order_type,
                        "session_bucket": rec.session_bucket,
                        "reprice_count": rec.reprice_count,
                        "final_status": rec.final_status,
                    })
                    await conn.execute(
                        "INSERT INTO signal_events "
                        "(strategy_id, ticker, session_date, event_ns, event_type, "
                        " lambda_hat, lambda_ref, epg_state_before, epg_state_after, notes) "
                        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                        rec.strategy_id, rec.ticker, session_date, time.time_ns(),
                        "CRASH_RECOVERY_CLOSE", None, None, None, None, notes,
                    )
        except Exception:
            log.critical("Crash recovery: DB reconcile transaction FAILED — re-raising", exc_info=True)
            raise


# ── Step 6: summary alert ─────────────────────────────────────────────────────

async def _send_summary(telegram, result: CrashRecoveryResult, tradable: bool) -> None:
    if telegram is None:
        return
    # Clean start (nothing found, nothing done) — stay silent.
    if not (
        result.had_open_positions
        or result.closed_tickers
        or result.deferred_tickers
        or result.stuck_tickers
        or result.error_tickers
    ):
        return

    problem = bool(result.deferred_tickers or result.stuck_tickers or result.error_tickers)
    prefix = "🔴" if problem else "🟡"
    bucket = current_session_bucket() if tradable else "outside_hours"

    def _line(label: str, tickers: list, flag: str = "") -> str:
        suffix = f"  ({', '.join(tickers)})" if tickers else ""
        return f"{label} {len(tickers)}{suffix}{flag}"

    msg = (
        f"{prefix} CRASH RECOVERY COMPLETE\n"
        f"Cancelled orders:  {len(result.cancelled_orders)}\n"
        f"{_line('Positions closed: ', result.closed_tickers)}\n"
        f"{_line('Deferred (no mkt):', result.deferred_tickers, '  ← manual review needed' if result.deferred_tickers else '')}\n"
        f"{_line('Stuck (no fill):  ', result.stuck_tickers, '  ← manual review needed' if result.stuck_tickers else '')}\n"
        f"{_line('Errors:           ', result.error_tickers, '  ← manual review needed' if result.error_tickers else '')}\n"
        f"Session at recovery: {bucket}"
    )
    await telegram.send_silent(msg)
