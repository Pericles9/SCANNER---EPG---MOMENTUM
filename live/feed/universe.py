"""Universe manager: WebSocket + per-ticker context fetch and signal loop lifecycle."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from live.session_clock import SessionClock

import aiohttp

from live.config import CFG
from live.feed.context import TickerContext
from live.feed.signal_loop import HeartbeatMonitor, signal_loop
from live.orders.risk import RiskState
from live.signals.context_fetch import fetch_context
from live.signals.live_state import LiveSignalState

log = logging.getLogger(__name__)

_POLYGON_WS_URL = "wss://socket.polygon.io/stocks"
_TICKER_QUEUE_SIZE = 1000

# Polygon stocks WebSocket sends `t` (SIP timestamp) in MILLISECONDS, but the
# entire downstream signal stack (live_state.update_trade/update_quote,
# _infer_session_bucket, signal_events.event_ns, ticks.sip_timestamp) was built
# against /v3/trades REST which returns sip_timestamp in NANOSECONDS. Without
# this conversion at the dispatch boundary, t_sec arithmetic goes catastrophically
# negative (mixing 1.78e12 with session_start_ns ≈ 1.78e18), the gate stays
# stuck in WARMUP for the entire session, and every entry gate fails silently.
_WS_MS_TO_NS = 1_000_000


def _normalize_ws_timestamps(item: dict) -> dict:
    """Convert Polygon WS event timestamps from ms → ns in-place. Returns item."""
    if "t" in item:
        item["t"] = int(item["t"]) * _WS_MS_TO_NS
    return item


class UniverseManager:
    """Manages per-ticker TickerContext lifecycle and Polygon WebSocket."""

    def __init__(
        self,
        order_queue: asyncio.Queue,
        risk_state: RiskState,
        polygon_api_key: str,
        hot_ticks: list,
        hot_quotes: list,
        hot_signal_events: list,
        hot_hawkes_refits: list,
        session_clock: "SessionClock",
        telegram=None,
    ) -> None:
        self._order_queue = order_queue
        self._risk_state = risk_state
        self._api_key = polygon_api_key
        self._hot_ticks = hot_ticks
        self._hot_quotes = hot_quotes
        self._hot_signal_events = hot_signal_events
        self._hot_hawkes_refits = hot_hawkes_refits
        self._clock = session_clock
        self._telegram = telegram

        self._universe: dict[str, TickerContext] = {}
        self._closed_today: set[str] = set()
        self._heartbeat = HeartbeatMonitor()

        # WebSocket outbound queue
        self._ws_send_queue: asyncio.Queue = asyncio.Queue()

        # Last WS message timestamp — mutable box for bot /status and /services
        self._ws_last_msg_t: list[float] = [0.0]

        # Active WS connection — assigned by _ws_connect_and_run, read by close_ws
        self._current_ws: Optional[aiohttp.ClientWebSocketResponse] = None

    async def run(self, universe_queue: asyncio.Queue) -> None:
        """Start WebSocket + process universe_queue in parallel."""
        ws_task = asyncio.create_task(self._ws_loop())
        hb_task = asyncio.create_task(
            self._heartbeat_loop()
        )
        try:
            async for ticker, scanner_ctx in self._iter_universe_queue(universe_queue):
                await self._add_ticker(ticker, scanner_ctx)
        finally:
            ws_task.cancel()
            hb_task.cancel()

    async def _iter_universe_queue(self, q: asyncio.Queue):
        while True:
            item = await q.get()
            yield item

    async def add_ticker(
        self,
        ticker: str,
        scanner_ctx: dict,
        existing_position: Optional[dict] = None,
    ) -> None:
        """Public entrypoint. Adds a ticker either from a scanner trigger (no
        existing_position) or from startup triage resuming a recovered IBKR
        position (existing_position is a {qty, avg_cost, open_ns} dict)."""
        await self._add_ticker(ticker, scanner_ctx, existing_position)

    async def _add_ticker(
        self,
        ticker: str,
        scanner_ctx: dict,
        existing_position: Optional[dict] = None,
    ) -> None:
        if ticker in self._universe:
            log.debug("Universe: %s already tracked", ticker)
            return
        if ticker in self._closed_today:
            log.debug("Universe: %s already closed today", ticker)
            return

        tag = " (resumed)" if existing_position is not None else ""
        log.info("Universe: adding %s%s", ticker, tag)
        state_ready = asyncio.Event()
        queue: asyncio.Queue = asyncio.Queue(maxsize=_TICKER_QUEUE_SIZE)

        # Create placeholder LiveSignalState — will be replaced after context fetch
        # We start with a stub and replace on context fetch completion
        task = asyncio.create_task(
            self._context_fetch_and_start(
                ticker, scanner_ctx, queue, state_ready, existing_position,
            )
        )

        # Subscribe WebSocket
        self._ws_send_queue.put_nowait({
            "action": "subscribe",
            "params": f"T.{ticker},Q.{ticker}",
        })

    async def _context_fetch_and_start(
        self,
        ticker: str,
        scanner_ctx: dict,
        queue: asyncio.Queue,
        state_ready: asyncio.Event,
        existing_position: Optional[dict] = None,
    ) -> None:
        try:
            ctx_result = await fetch_context(
                ticker=ticker,
                session_clock=self._clock,
                scanner_context=scanner_ctx,
                polygon_api_key=self._api_key,
                account_equity=self._risk_state.account_equity,
                theoretical_equity=self._risk_state.theoretical_equity,
            )
        except Exception:
            log.exception("%s: context fetch failed", ticker)
            state_ready.set()
            return

        live_state = LiveSignalState(
            ticker=ticker,
            ctx=ctx_result,
            scanner_context=scanner_ctx,
            session_date=self._clock.date,
        )

        # If resuming from an existing position (startup triage), mark the state
        # in_position so the signal loop dispatches to _check_exits, not _check_entry.
        if existing_position is not None:
            from live.feed.market_status import current_session_bucket
            live_state.record_entry(
                session_bkt=current_session_bucket(),
                i_entry=0.5,  # neutral — original i_entry was lost across restart
            )
            log.info(
                "%s: resumed with existing position (qty=%d avg_cost=%.4f)",
                ticker, existing_position.get("qty", 0),
                existing_position.get("avg_cost", 0.0),
            )

        async def _sf_disqualify_callback() -> None:
            await self.remove_ticker(ticker, "sf_disqualified")
            if self._telegram is not None:
                try:
                    asyncio.create_task(self._telegram.send_silent(
                        f"{ticker}: SF disqualified after {CFG.setup_filter.removal_bars} consecutive failing bars — removed from universe"
                    ))
                except Exception:
                    pass

        loop_task = asyncio.create_task(
            signal_loop(
                ctx=TickerContext(
                    ticker=ticker,
                    queue=queue,
                    signal_state=live_state,
                    task=asyncio.current_task(),  # type: ignore
                    state_ready=state_ready,
                    scanner_context=scanner_ctx,
                ),
                order_queue=self._order_queue,
                risk_state=self._risk_state,
                hot_ticks=self._hot_ticks,
                hot_quotes=self._hot_quotes,
                hot_signal_events=self._hot_signal_events,
                hot_hawkes_refits=self._hot_hawkes_refits,
                heartbeat=self._heartbeat,
                session_clock=self._clock,
                disqualify_callback=_sf_disqualify_callback,
            )
        )

        ticker_ctx = TickerContext(
            ticker=ticker,
            queue=queue,
            signal_state=live_state,
            task=loop_task,
            state_ready=state_ready,
            scanner_context=scanner_ctx,
        )
        self._universe[ticker] = ticker_ctx
        state_ready.set()
        log.info("%s: context fetch complete, signal loop started", ticker)

    async def remove_ticker(self, ticker: str, close_reason: str = "session_close") -> None:
        """Remove ticker from universe. Order: pop → cancel → unsubscribe → export.

        closed_today lockout is currently disabled: no close reason adds the ticker
        to closed_today, so any removed ticker (scanner_dropoff, session_close,
        EPG_CLOSE, EXIT_D, LULD, EOD) can re-enter the universe if it qualifies again.
        The closed_today set is retained (kept empty) so gate checks and bot readouts
        stay valid.
        """
        ctx = self._universe.pop(ticker, None)
        if ctx is None:
            return
        ctx.task.cancel()
        self._ws_send_queue.put_nowait({
            "action": "unsubscribe",
            "params": f"T.{ticker},Q.{ticker}",
        })
        self._heartbeat.remove(ticker)
        log.info("Universe: removed %s", ticker)

        # Session export — non-blocking; errors logged, not re-raised
        intraday_pct = ctx.signal_state.intraday_pct if ctx.signal_state else None
        theo_equity = self._risk_state.theoretical_equity
        asyncio.create_task(
            _export_ticker_session(ticker, self._clock.date, intraday_pct, theo_equity, close_reason)
        )

    async def _dispatch(self, msg: dict) -> None:
        ticker = msg.get("sym")
        if ticker is None or ticker not in self._universe:
            return
        ctx = self._universe[ticker]
        try:
            ctx.queue.put_nowait(msg)
        except asyncio.QueueFull:
            log.warning("%s: queue full, dropping tick", ticker)

    async def _ws_loop(self) -> None:
        """Maintain Polygon WebSocket connection with exponential backoff.

        Backoff: 1 → 2 → 4 → 8 → 16 → 30s max.
        Dead man's switch: if WS has been down > 60s with an open position,
        enqueue FlattenAllRequest once to protect capital.
        Telegram alerts fire on first disconnect and on successful reconnect.
        """
        _WS_DISCONNECT_FLATTEN_S = 60.0
        _MAX_BACKOFF_S = 30.0
        delay = 1.0
        _flatten_fired = False
        _disconnected: list[bool] = [False]

        while True:
            prev_last_msg_t = self._ws_last_msg_t[0]
            try:
                await self._ws_connect_and_run(_disconnected)
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._ws_last_msg_t[0] > prev_last_msg_t:
                    delay = 1.0
                    _flatten_fired = False

                down_secs = (
                    time.monotonic() - self._ws_last_msg_t[0]
                    if self._ws_last_msg_t[0] > 0
                    else 0.0
                )
                log.exception(
                    "WebSocket error — reconnecting in %.0fs (down %.0fs)", delay, down_secs
                )

                if not _disconnected[0]:
                    _disconnected[0] = True
                    if self._telegram is not None:
                        asyncio.create_task(
                            self._telegram.send_silent(
                                f"Polygon WS disconnected — reconnecting (down {down_secs:.0f}s)"
                            )
                        )

                if (
                    not _flatten_fired
                    and down_secs >= _WS_DISCONNECT_FLATTEN_S
                    and self._risk_state.open_positions
                ):
                    _flatten_fired = True
                    log.critical(
                        "WebSocket down %.0fs with open position — triggering flatten-all",
                        down_secs,
                    )
                    from live.orders.risk import FlattenAllRequest
                    self._order_queue.put_nowait(FlattenAllRequest(reason="ws_disconnect_60s"))

                await asyncio.sleep(delay)
                delay = min(delay * 2, _MAX_BACKOFF_S)

    async def _ws_connect_and_run(self, disconnected: list[bool]) -> None:
        _reconnect_alerted = False
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(_POLYGON_WS_URL) as ws:
                self._current_ws = ws
                # Auth
                await ws.send_str(json.dumps({"action": "auth", "params": self._api_key}))

                # Re-subscribe all active tickers after reconnect
                if self._universe:
                    subs = ",".join(
                        f"T.{t},Q.{t}" for t in self._universe
                    )
                    await ws.send_str(json.dumps({"action": "subscribe", "params": subs}))

                sender_task = asyncio.create_task(self._ws_sender(ws))
                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            for item in json.loads(msg.data):
                                ev = item.get("ev")
                                if ev in ("T", "Q"):
                                    _normalize_ws_timestamps(item)
                                    self._ws_last_msg_t[0] = time.monotonic()
                                    if disconnected[0] and not _reconnect_alerted:
                                        _reconnect_alerted = True
                                        disconnected[0] = False
                                        if self._telegram is not None:
                                            asyncio.create_task(
                                                self._telegram.send_silent(
                                                    "Polygon WS reconnected — feed restored"
                                                )
                                            )
                                    await self._dispatch(item)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                finally:
                    sender_task.cancel()
                    self._current_ws = None

    async def _ws_sender(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            msg = await self._ws_send_queue.get()
            await ws.send_str(json.dumps(msg))

    @property
    def ws_last_msg_t(self) -> list[float]:
        return self._ws_last_msg_t

    @property
    def closed_today(self) -> set:
        return self._closed_today

    async def handle_snapshot_dropoffs(self, qualifying_tickers: set) -> None:
        """Remove universe tickers absent from the current scanner snapshot (no open position)."""
        for ticker in list(self._universe.keys()):
            if ticker not in qualifying_tickers and not self._risk_state.has_position(ticker):
                log.info("[scanner] drop-off: %s not in snapshot — removing from universe", ticker)
                await self.remove_ticker(ticker, close_reason="scanner_dropoff")

    async def close_ws(self) -> None:
        """Close the active Polygon WebSocket. _ws_loop will reconnect (idle if no tickers)."""
        if self._current_ws is None:
            return
        ws = self._current_ws
        if ws.closed:
            return
        try:
            await ws.close()
            log.info("Polygon WS: closed by session_close")
        except Exception:
            log.exception("Polygon WS close failed")

    async def session_close(self, order_queue: asyncio.Queue, risk_state, telegram) -> None:
        """20:00 ET daily session close: flatten, sweep universe, reset daily state, close WS."""
        log.info("Session close: starting 20:00 ET sweep")

        if risk_state.open_positions:
            tickers = list(risk_state.open_positions.keys())
            log.critical(
                "Session close: %d open position(s) at 20:00 ET — force-flattening: %s",
                len(tickers), tickers,
            )
            from live.orders.risk import FlattenAllRequest
            order_queue.put_nowait(FlattenAllRequest(reason="session_close_20et"))

        for ticker in list(self._universe.keys()):
            await self.remove_ticker(ticker, close_reason="session_close")

        self._closed_today.clear()
        risk_state.daily_pnl = 0.0
        risk_state._loss_limit_hit = False

        await self.close_ws()

        self._clock.roll()
        log.info("Session date advanced to %s", self._clock.date)

        log.info("Session close complete. Universe cleared, closed_today reset, WS closed.")
        if telegram is not None:
            asyncio.create_task(
                telegram.send_silent(
                    "Session closed. Universe cleared. WS disconnected. Ready for 04:00 ET open."
                )
            )

    async def _heartbeat_loop(self) -> None:
        from live.feed.signal_loop import heartbeat_monitor
        await heartbeat_monitor(
            universe=self._universe,
            order_queue=self._order_queue,
            risk_state=self._risk_state,
            heartbeat=self._heartbeat,
        )


async def _export_ticker_session(
    ticker: str,
    session_date,
    intraday_pct,
    theoretical_equity_end: float,
    close_reason: str,
) -> None:
    from live.export.session import export_session
    try:
        await export_session(
            ticker=ticker,
            session_date=session_date,
            intraday_pct=intraday_pct,
            theoretical_equity_end=theoretical_equity_end,
            close_reason=close_reason,
        )
    except Exception:
        log.exception("Session export failed for %s", ticker)
