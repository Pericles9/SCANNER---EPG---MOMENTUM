"""Startup position triage — Process 2.

Runs between IBKR reconciliation and the start of scanner polling. For every
open position in the `positions` table, decide automatically:

  * CLOSE — EPG state is FAIL or UNRESOLVABLE (we don't trust the position)
  * RESUME — EPG state is PASS or INACTIVE (signal monitoring takes over)
  * WATCH — IBKR returns no valid quote (halted/suspended); wait for resume

Triage must complete before scanner polling starts. Each position is processed
concurrently via asyncio.gather. Close requests go on order_queue only. Resumes
are handled by calling UniverseManager.add_ticker(ticker, existing_position=).
"""
from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from backtest.data.loaders.trades import _session_ns_bounds
# backtest.runner import seeds sys.path with /app/backtest so bare 'core.*' works
from backtest.runner import session_bucket  # noqa: F401

from core.epg.anchor import EventAnchor
from core.epg.gate import GateState, ParticipationGate
from core.hawkes.engine import HawkesEngine

from live.config import CFG
from live.orders.risk import OrderRequest

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_NS_PER_SEC = 1_000_000_000
_WATCHER_POLL_S = 5.0


class EPGTriageOutcome(enum.Enum):
    """Resolved EPG state after replaying the live tick history from the last
    refit forward. Distinct from GateState (which lacks UNRESOLVABLE).
    """
    PASS = "PASS"
    FAIL = "FAIL"
    INACTIVE = "INACTIVE"        # includes WARMUP, INACTIVE — same handling
    UNRESOLVABLE = "UNRESOLVABLE"  # no refit row or replay raised


@dataclass
class _Position:
    """One row from the positions table."""
    ticker: str
    qty: int
    avg_entry_price: float
    open_ns: Optional[int]


# ── Public entrypoint ────────────────────────────────────────────────────────

async def startup_position_triage(
    pool,
    ibkr,
    order_queue: asyncio.Queue,
    universe_mgr,
    telegram,
    session_date: date,
    hot_signal_events: Optional[list] = None,
    risk_state=None,
) -> None:
    """Triage every open position. Blocks until each per-ticker decision is
    made; close-on-resume watchers continue as background tasks past return.

    Caller responsibility: do NOT start scanner_monitor until this returns.
    """
    positions = await _load_open_positions(pool, session_date)

    # Merge IBKR positions not in the DB — happens when SKIP_POSITION_CHECK=true
    # seeds risk_state.open_positions from IBKR without writing to the positions table.
    if risk_state is not None:
        db_tickers = {p.ticker for p in positions}
        for ticker, pos_info in risk_state.open_positions.items():
            if ticker not in db_tickers:
                log.warning(
                    "Triage: %s in IBKR risk_state but not in positions table — "
                    "adding to triage list (likely SKIP_POSITION_CHECK=true seed)",
                    ticker,
                )
                positions.append(_Position(
                    ticker=ticker,
                    qty=pos_info["qty"],
                    avg_entry_price=pos_info.get("avg_cost", 0.0),
                    open_ns=None,
                ))
    n = len(positions)

    if n == 0:
        log.info("Startup triage: 0 open positions — nothing to do")
        if telegram is not None:
            await telegram.send_silent("Startup triage: 0 open positions")
        return

    log.info("Startup triage: %d open position(s) found — evaluating", n)
    if telegram is not None:
        tickers_csv = ", ".join(sorted(p.ticker for p in positions))
        await telegram.send_silent(
            f"Startup triage: {n} open position(s) — {tickers_csv}"
        )

    # Compute EOD once — same boundary for all watchers
    session_end_ns = _session_end_ns(session_date)

    # Run all triage tasks concurrently
    results = await asyncio.gather(
        *(
            _triage_one(
                pos=pos,
                pool=pool,
                ibkr=ibkr,
                order_queue=order_queue,
                universe_mgr=universe_mgr,
                telegram=telegram,
                session_date=session_date,
                session_end_ns=session_end_ns,
                hot_signal_events=hot_signal_events,
            )
            for pos in positions
        ),
        return_exceptions=True,
    )

    closed: list[str] = []
    resumed: list[str] = []
    watching: list[str] = []
    errored: list[str] = []
    for pos, outcome in zip(positions, results):
        if isinstance(outcome, Exception):
            log.exception("Triage task crashed for %s", pos.ticker, exc_info=outcome)
            errored.append(pos.ticker)
        elif outcome == "closed":
            closed.append(pos.ticker)
        elif outcome == "resumed":
            resumed.append(pos.ticker)
        elif outcome == "watching":
            watching.append(pos.ticker)

    # Approximate unrealised P&L — fire all snapshot_quote calls concurrently
    quotes = await asyncio.gather(
        *(ibkr.snapshot_quote(pos.ticker) for pos in positions),
        return_exceptions=True,
    )
    total_unreal = 0.0
    for pos, q in zip(positions, quotes):
        if isinstance(q, Exception):
            continue
        bid, ask = q
        if bid > 0 or ask > 0:
            mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else (bid or ask)
            total_unreal += (mid - pos.avg_entry_price) * pos.qty

    summary = (
        f"Startup triage complete\n"
        f"  Closed:   {len(closed)}{' (' + ', '.join(closed) + ')' if closed else ''}\n"
        f"  Resumed:  {len(resumed)}{' (' + ', '.join(resumed) + ')' if resumed else ''}\n"
        f"  Watching: {len(watching)}{' (' + ', '.join(watching) + ')' if watching else ''}"
        + (f"\n  Errored:  {len(errored)} ({', '.join(errored)})" if errored else "")
        + f"\n  Unrealised P&L (approx): ${total_unreal:+.2f}"
    )
    log.info(summary.replace("\n", " | "))
    if telegram is not None:
        await telegram.send_silent(summary)


# ── Per-position triage ─────────────────────────────────────────────────────

async def _triage_one(
    pos: _Position,
    pool,
    ibkr,
    order_queue: asyncio.Queue,
    universe_mgr,
    telegram,
    session_date: date,
    session_end_ns: int,
    hot_signal_events: Optional[list],
) -> str:
    """Process a single position. Returns 'closed' | 'resumed' | 'watching'."""
    # Step 1 — IBKR price validity
    bid, ask = await ibkr.snapshot_quote(pos.ticker)
    if bid == 0.0 and ask == 0.0:
        # No live quote — send a liberal limit using avg_entry_price as reference.
        # Better to be in the queue at a known price than wait for a quote that
        # may not arrive before EOD in extended hours.
        log.warning(
            "Triage %s: no IBKR quote — closing at avg_cost limit (qty=%d @ $%.4f)",
            pos.ticker, pos.qty, pos.avg_entry_price,
        )
        return await _queue_triage_close(
            pos, "EPG_NO_QUOTE_ON_RESTART", "TRIAGE_CLOSE_NO_QUOTE",
            bid=pos.avg_entry_price, ask=pos.avg_entry_price,
            order_queue=order_queue, telegram=telegram,
            session_date=session_date, hot_signal_events=hot_signal_events,
        )

    # Step 2 — resolve EPG state from DB
    outcome = await _resolve_epg_state(pos.ticker, session_date, pool)

    # Step 3 — act
    if outcome == EPGTriageOutcome.FAIL:
        return await _queue_triage_close(
            pos, "EPG_FAIL_ON_RESTART", "TRIAGE_CLOSE_EPG_FAIL",
            bid, ask, order_queue, telegram, session_date, hot_signal_events,
        )

    if outcome == EPGTriageOutcome.UNRESOLVABLE:
        return await _queue_triage_close(
            pos, "EPG_UNRESOLVABLE_ON_RESTART", "TRIAGE_CLOSE_UNRESOLVABLE",
            bid, ask, order_queue, telegram, session_date, hot_signal_events,
        )

    # PASS or INACTIVE → resume monitoring
    event_type = (
        "TRIAGE_RESUME_PASS" if outcome == EPGTriageOutcome.PASS
        else "TRIAGE_RESUME_INACTIVE"
    )
    log.info(
        "Triage %s: EPG=%s — resuming signal monitoring (qty=%d @ $%.4f)",
        pos.ticker, outcome.value, pos.qty, pos.avg_entry_price,
    )
    if telegram is not None:
        await telegram.send_silent(
            f"Triage {pos.ticker}: EPG={outcome.value} — resuming "
            f"({pos.qty}sh @ ${pos.avg_entry_price:.2f})"
        )
    _record_event(
        hot_signal_events, pos.ticker, session_date,
        event_type, note=f"qty={pos.qty}",
    )

    # Hand off to universe manager. Scanner context is synthetic — we have
    # no live scanner snapshot for a position carried over from prior runs.
    scanner_ctx = _synthetic_scanner_context()
    await universe_mgr.add_ticker(
        pos.ticker,
        scanner_ctx,
        existing_position={
            "qty": pos.qty,
            "avg_cost": pos.avg_entry_price,
            "open_ns": pos.open_ns,
        },
    )
    return "resumed"


async def _queue_triage_close(
    pos: _Position,
    reason: str,
    event_type: str,
    bid: float,
    ask: float,
    order_queue: asyncio.Queue,
    telegram,
    session_date: date,
    hot_signal_events: Optional[list],
) -> str:
    """Build and enqueue a SELL close for a triaged position. Returns 'closed'."""
    bkt = _current_session_bucket()
    req = _build_close_request(pos, reason, bkt, bid, ask)
    order_queue.put_nowait(req)
    log.warning(
        "Triage %s: closing (reason=%s, qty=%d, bkt=%s, limit=%s)",
        pos.ticker, reason, pos.qty, bkt,
        f"${req.limit_price:.2f}" if req.limit_price else "MKT",
    )
    if telegram is not None:
        await telegram.send_silent(
            f"Triage {pos.ticker}: closing — {reason} (qty={pos.qty})"
        )
    _record_event(
        hot_signal_events, pos.ticker, session_date,
        event_type, note=f"qty={pos.qty} reason={reason}",
    )
    return "closed"


# ── Close-on-resume watcher ─────────────────────────────────────────────────

async def _close_on_resume_watcher(
    pos: _Position,
    ibkr,
    order_queue: asyncio.Queue,
    telegram,
    session_end_ns: int,
    session_date: date,
    hot_signal_events: Optional[list],
) -> None:
    """Poll IBKR every _WATCHER_POLL_S; close on first valid quote or at EOD."""
    while True:
        # EOD check first so a halt that extends past 8pm ET still gets flattened
        if time.time_ns() >= session_end_ns:
            bkt = _current_session_bucket()
            # Synthetic bid for limit calc when we never saw a quote
            req = _build_close_request(
                pos, "CLOSE_FORCED_EOD", bkt,
                bid=pos.avg_entry_price, ask=pos.avg_entry_price,
            )
            order_queue.put_nowait(req)
            log.warning(
                "Triage watcher %s: EOD reached without quote — forcing close (qty=%d)",
                pos.ticker, pos.qty,
            )
            if telegram is not None:
                await telegram.send_silent(
                    f"Triage watcher {pos.ticker}: EOD forced close (qty={pos.qty})"
                )
            _record_event(
                hot_signal_events, pos.ticker, session_date,
                "TRIAGE_WATCHER_EOD_FORCED", note=f"qty={pos.qty}",
            )
            return

        bid, ask = await ibkr.snapshot_quote(pos.ticker)
        if bid > 0 or ask > 0:
            bkt = _current_session_bucket()
            req = _build_close_request(pos, "CLOSE_ON_RESUME", bkt, bid, ask)
            order_queue.put_nowait(req)
            log.info(
                "Triage watcher %s: quote resumed (bid=%.2f ask=%.2f) — closing (qty=%d)",
                pos.ticker, bid, ask, pos.qty,
            )
            if telegram is not None:
                await telegram.send_silent(
                    f"Triage watcher {pos.ticker}: resumed, closing (qty={pos.qty})"
                )
            _record_event(
                hot_signal_events, pos.ticker, session_date,
                "TRIAGE_WATCHER_CLOSED", note=f"qty={pos.qty}",
            )
            return

        await asyncio.sleep(_WATCHER_POLL_S)


# ── EPG state reconstruction ────────────────────────────────────────────────

async def _resolve_epg_state(
    ticker: str, session_date: date, pool,
) -> EPGTriageOutcome:
    """Reconstruct gate state by replaying ticks since the last refit.

    Diverges from spec pseudocode in three places — the spec's API references
    don't match the real code:

      * Hawkes intensity comes from HawkesEngine.update(t_sec, side), not from
        KalmanIntensityEstimator (which takes only lambda_ref, not refit params).
      * EventAnchor input is lambda_total (lambda_buy + lambda_sell), matching
        the Bug #1 fix in live_state.py.
      * ParticipationGate.update takes (dollar_vol, t_sec), not (lv, lr, t_sec).
        Dollar volume comes from ticks.price * ticks.size.
    """
    async with pool.acquire() as conn:
        refit = await conn.fetchrow(
            """
            SELECT mu_buy, mu_sell, alpha_buy_self, alpha_sell_self, refit_ns
            FROM hawkes_refits
            WHERE strategy_id = $1 AND ticker = $2 AND session_date = $3
            ORDER BY refit_ns DESC
            LIMIT 1
            """,
            CFG.strategy_id, ticker, session_date,
        )

        if refit is None:
            return EPGTriageOutcome.UNRESOLVABLE

        try:
            ticks = await conn.fetch(
                """
                SELECT sip_timestamp, side, price, size
                FROM ticks
                WHERE ticker = $1 AND session_date = $2 AND sip_timestamp > $3
                ORDER BY sip_timestamp
                """,
                ticker, session_date, refit["refit_ns"],
            )

            # If t_event has been logged for this ticker today, use it; otherwise
            # the anchor will re-fire from the replay.
            t_event_row = await conn.fetchrow(
                """
                SELECT event_ns FROM signal_events
                WHERE strategy_id = $1 AND ticker = $2 AND session_date = $3
                  AND event_type = 'T_EVENT_FIRE'
                ORDER BY event_ns ASC LIMIT 1
                """,
                CFG.strategy_id, ticker, session_date,
            )

            session_start_ns, _ = _session_ns_bounds(session_date.isoformat())
            lambda_ref = (
                float(refit["mu_buy"]) + float(refit["mu_sell"])
            ) or (CFG.hawkes.mu_buy + CFG.hawkes.mu_sell)

            engine = HawkesEngine(
                beta_mle=CFG.hawkes.beta,
                alpha_self_buy=float(refit["alpha_buy_self"] or 0.0),
                alpha_cross_buy=0.0,
                mu_buy=float(refit["mu_buy"]),
                mu_sell=float(refit["mu_sell"]),
                lambda_ref=lambda_ref,
                alpha_self_sell=float(refit["alpha_sell_self"] or 0.0),
                alpha_cross_sell=0.0,
            )
            anchor = EventAnchor(
                lambda_ref=lambda_ref,
                k_multiplier=CFG.epg.t_event_threshold,
            )
            gate = ParticipationGate(
                half_life_seconds=CFG.epg.window_close_sec,
                peak_threshold_p=CFG.epg.lambda_v_threshold,
            )
            if t_event_row is not None:
                # Seed t_event so warmup math is correct
                t_event_sec = (t_event_row["event_ns"] - session_start_ns) / _NS_PER_SEC
                anchor._t_event = t_event_sec
                anchor._fired = True
                gate.activate(t_event_sec)

            current_state = gate.state if gate.t_event is not None else GateState.INACTIVE
            for tick in ticks:
                ts_ns = tick["sip_timestamp"]
                # Side from DB is +1/-1 (post-Bug-#2); coerce to int defensively
                side = int(tick["side"]) if tick["side"] is not None else 1
                t_sec = (ts_ns - session_start_ns) / _NS_PER_SEC

                hs = engine.update(t_sec, side)
                t_ev = anchor.update(hs.lambda_total, t_sec)
                if t_ev is not None and gate.t_event is None:
                    gate.activate(t_ev)

                if gate.t_event is not None:
                    dollar_vol = float(tick["price"]) * float(tick["size"])
                    current_state = gate.update(dollar_vol, t_sec)

            if current_state == GateState.PASS:
                return EPGTriageOutcome.PASS
            if current_state == GateState.FAIL:
                return EPGTriageOutcome.FAIL
            return EPGTriageOutcome.INACTIVE

        except Exception:
            log.exception("EPG reconstruction failed for %s", ticker)
            return EPGTriageOutcome.UNRESOLVABLE


# ── DB / utility helpers ────────────────────────────────────────────────────

async def _load_open_positions(pool, session_date: date) -> list[_Position]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ticker, qty, avg_entry_price, open_ns
            FROM positions
            WHERE strategy_id = $1 AND session_date = $2 AND qty != 0
            ORDER BY ticker
            """,
            CFG.strategy_id, session_date,
        )
    return [
        _Position(
            ticker=r["ticker"],
            qty=int(r["qty"]),
            avg_entry_price=float(r["avg_entry_price"]) if r["avg_entry_price"] else 0.0,
            open_ns=int(r["open_ns"]) if r["open_ns"] else None,
        )
        for r in rows
    ]


def _session_end_ns(session_date: date) -> int:
    """End-of-session epoch ns (8pm ET, per system architecture)."""
    _, end_ns = _session_ns_bounds(session_date.isoformat())
    return end_ns


def _current_session_bucket() -> str:
    """Determine current session bucket from wall-clock ET time."""
    now_et = datetime.now(_ET)
    sec = now_et.hour * 3600 + now_et.minute * 60 + now_et.second
    if sec < 9 * 3600 + 30 * 60:
        return "pre_market"
    if sec < 16 * 3600:
        return "regular_hours"
    return "post_market"


def _synthetic_scanner_context() -> dict:
    """Empty scanner context for resumed positions (no live scan exists)."""
    return {
        "ticker": "",
        "pct_change": 0.0,
        "scanner_rank": 0,
        "scanner_n": 0,
        "scanner_heat": 0.0,
        "scanner_quartile": 0,
        "snapshot_ns": 0,
    }


def _build_close_request(
    pos: _Position,
    reason: str,
    bkt: str,
    bid: float,
    ask: float,
) -> OrderRequest:
    """Build a SELL OrderRequest for a triaged close.

    Pass an explicit qty (not the sentinel 0) — triage knows the exact size
    from the positions table. For extended hours, set a liberal limit price
    so the order actually fills.
    """
    # Always use a limit price — submit() requires one for all session buckets.
    bid_ref = bid if bid > 0 else (
        ask - CFG.order_execution.extended_exit_offset if ask > 0
        else pos.avg_entry_price
    )
    limit_price = round(max(0.01, bid_ref - CFG.order_execution.extended_exit_offset), 2)
    expected_price = limit_price
    return OrderRequest(
        ticker=pos.ticker,
        side="SELL",
        qty=pos.qty,
        session_bucket=bkt,
        is_entry=False,
        exit_reason=reason,
        limit_price=limit_price,
        intraday_pct=0.0,
        expected_price=expected_price,
    )


def _record_event(
    hot_signal_events: Optional[list],
    ticker: str,
    session_date: date,
    event_type: str,
    note: str = "",
) -> None:
    """Append a signal_events tuple via the existing batch-writer hot buffer.

    Tuple layout matches db/models.SIGNAL_EVENTS_COLUMNS.
    """
    if hot_signal_events is None:
        return
    hot_signal_events.append((
        CFG.strategy_id, ticker, session_date,
        time.time_ns(), event_type,
        None, None,             # lambda_hat, lambda_ref — n/a for triage
        None, None,             # epg_state_before, epg_state_after — n/a
        note or None,
    ))
