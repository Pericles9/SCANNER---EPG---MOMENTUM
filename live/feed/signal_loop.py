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

    if signal in ("EXIT_D", "LULD", "EPG_CLOSE", "VWAP_CLOSE", "VWAP_CROSS", "HARD_STOP"):
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
    ws_last_msg_t: Optional[list] = None,
    telegram=None,
) -> None:
    """Periodic dead man's switch check — halt-aware.

    A real exchange halt produces exactly the same per-ticker tick gap as feed death,
    so flattening on a stale heartbeat alone would force-exit on a halt (violating the
    locked rule "do not force-exit on halt alone"). Disambiguation per stale ticker
    with an open position:

      • Real LULD halt (`is_halted()`) or halt-suspected (LULD still arriving while T/Q
        stopped — covers NYSE/AMEX where indicators 17/18 don't exist) → HOLD, alert
        once, keep a grace timer. Escalate a single FlattenTicker only if the hold
        exceeds `CFG.luld.max_halt_hold_s`.
      • Otherwise true symbol-level feed death (no T/Q/LULD) while the global WS is
        healthy → FlattenTicker. If the global WS is also stale, defer to the
        WS-disconnect path (it owns that scenario).
    """
    from live.feed import market_status
    _halt_since: dict[str, float] = {}   # ticker → monotonic time hold began
    _alerted: set[str] = set()
    _escalated: set[str] = set()
    while True:
        await asyncio.sleep(_HEARTBEAT_CHECK_INTERVAL_S)
        # The dead man's switch guards against a frozen feed during the session.
        # When the market is closed there is legitimately no feed, so a stale
        # heartbeat is expected — do not flatten (it can't fill and only spams).
        if not market_status.is_tradable_now():
            continue

        now = time.monotonic()
        # The ws_healthy gate only *suppresses* a flatten when we positively know the
        # global WS is down (the WS-disconnect path owns that). With no WS box, or a
        # fresh WS, default to healthy so true symbol feed-death still flattens.
        if ws_last_msg_t is None:
            ws_age = 0.0
            ws_healthy = True
        else:
            ws_age = now - ws_last_msg_t[0] if ws_last_msg_t[0] > 0 else float("inf")
            ws_healthy = ws_age < CFG.risk.dead_man_timeout_s
        stale = set(heartbeat.stale_tickers(CFG.risk.dead_man_timeout_s))

        _dead_mans_switch_pass(
            stale=stale, universe=universe, risk_state=risk_state, order_queue=order_queue,
            now=now, ws_healthy=ws_healthy, ws_age=ws_age,
            halt_since=_halt_since, alerted=_alerted, escalated=_escalated, telegram=telegram,
        )


def _dead_mans_switch_pass(
    stale: set,
    universe: dict,
    risk_state: RiskState,
    order_queue: asyncio.Queue,
    now: float,
    ws_healthy: bool,
    ws_age: float,
    halt_since: dict,
    alerted: set,
    escalated: set,
    telegram=None,
) -> None:
    """One halt-aware dead-man's-switch pass over the stale tickers. Extracted for
    testability; mutates `order_queue` and the bookkeeping sets/dicts in place."""
    # Forget bookkeeping for tickers whose feed recovered (no longer stale).
    for t in [t for t in halt_since if t not in stale]:
        halt_since.pop(t, None)
        alerted.discard(t)
        escalated.discard(t)

    for ticker in stale:
        if not risk_state.has_position(ticker):
            continue

        ctx = universe.get(ticker)
        ss = ctx.signal_state if ctx else None
        halted = bool(ss is not None and hasattr(ss, "is_halted") and ss.is_halted())
        luld_seen = getattr(ss, "luld_last_seen", 0.0) if ss is not None else 0.0
        luld_fresh = bool(luld_seen and (now - luld_seen) < CFG.risk.dead_man_timeout_s)
        halt_or_suspected = halted or luld_fresh

        if halt_or_suspected:
            start = halt_since.setdefault(ticker, now)
            held = now - start
            if held >= CFG.luld.max_halt_hold_s and ticker not in escalated:
                escalated.add(ticker)
                log.critical(
                    "HALT CAP: %s held %.0fs >= max_halt_hold_s (%.0fs) — flattening ticker",
                    ticker, held, CFG.luld.max_halt_hold_s,
                )
                if telegram is not None:
                    asyncio.create_task(telegram.send_silent(
                        f"HALT CAP: {ticker} halted/stalled {held:.0f}s — flattening (cap exceeded)."
                    ))
                order_queue.put_nowait(
                    FlattenTickerRequest(ticker=ticker, reason="halt_hold_cap_exceeded")
                )
            elif ticker not in alerted:
                alerted.add(ticker)
                kind = "HALT" if halted else "HALT-SUSPECTED"
                log.warning(
                    "%s: %s — holding (T/Q gap is a halt, not feed death; LULD active)",
                    ticker, kind,
                )
                if telegram is not None:
                    asyncio.create_task(telegram.send_silent(
                        f"{kind} — holding {ticker} (feed gap is a halt, not feed death)."
                    ))
            continue   # never force-exit a halted / halt-suspected ticker

        # No halt evidence. Flatten only on true symbol-level feed death while the
        # global WS is healthy; otherwise the WS-disconnect path owns it.
        if ws_healthy:
            log.critical(
                "DEAD MAN'S SWITCH: no T/Q/LULD for %s in %ds (global WS healthy) — flattening ticker only",
                ticker, int(CFG.risk.dead_man_timeout_s),
            )
            order_queue.put_nowait(
                FlattenTickerRequest(ticker=ticker, reason="dead_mans_switch")
            )
        else:
            log.warning(
                "%s: stale but global WS also down (age %.0fs) — deferring to WS-disconnect path",
                ticker, ws_age,
            )
