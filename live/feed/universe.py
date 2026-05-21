"""Universe manager: WebSocket + per-ticker context fetch and signal loop lifecycle."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date

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
        session_date: date,
    ) -> None:
        self._order_queue = order_queue
        self._risk_state = risk_state
        self._api_key = polygon_api_key
        self._hot_ticks = hot_ticks
        self._hot_quotes = hot_quotes
        self._hot_signal_events = hot_signal_events
        self._hot_hawkes_refits = hot_hawkes_refits
        self._session_date = session_date

        self._universe: dict[str, TickerContext] = {}
        self._closed_today: set[str] = set()
        self._heartbeat = HeartbeatMonitor()

        # WebSocket outbound queue
        self._ws_send_queue: asyncio.Queue = asyncio.Queue()

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

    async def _add_ticker(self, ticker: str, scanner_ctx: dict) -> None:
        if ticker in self._universe:
            log.debug("Universe: %s already tracked", ticker)
            return
        if ticker in self._closed_today:
            log.debug("Universe: %s already closed today", ticker)
            return

        log.info("Universe: adding %s", ticker)
        state_ready = asyncio.Event()
        queue: asyncio.Queue = asyncio.Queue(maxsize=_TICKER_QUEUE_SIZE)

        # Create placeholder LiveSignalState — will be replaced after context fetch
        # We start with a stub and replace on context fetch completion
        task = asyncio.create_task(
            self._context_fetch_and_start(ticker, scanner_ctx, queue, state_ready)
        )

        # Subscribe WebSocket
        await self._ws_send_queue.put({
            "action": "subscribe",
            "params": f"T.{ticker},Q.{ticker}",
        })

    async def _context_fetch_and_start(
        self,
        ticker: str,
        scanner_ctx: dict,
        queue: asyncio.Queue,
        state_ready: asyncio.Event,
    ) -> None:
        try:
            ctx_result = await fetch_context(
                ticker=ticker,
                session_date=self._session_date,
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
            session_date=self._session_date,
        )

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
                session_date=self._session_date,
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
        """Remove ticker from universe. Order: pop → cancel → unsubscribe → export."""
        ctx = self._universe.pop(ticker, None)
        if ctx is None:
            return
        ctx.closed_today = True
        self._closed_today.add(ticker)
        ctx.task.cancel()
        await self._ws_send_queue.put({
            "action": "unsubscribe",
            "params": f"T.{ticker},Q.{ticker}",
        })
        self._heartbeat.remove(ticker)
        log.info("Universe: removed %s", ticker)

        # Session export — non-blocking; errors logged, not re-raised
        intraday_pct = ctx.signal_state.intraday_pct if ctx.signal_state else None
        theo_equity = self._risk_state.theoretical_equity
        asyncio.create_task(
            _export_ticker_session(ticker, self._session_date, intraday_pct, theo_equity, close_reason)
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
        """Maintain Polygon WebSocket connection."""
        while True:
            try:
                await self._ws_connect_and_run()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("WebSocket error — reconnecting in 5s")
                await asyncio.sleep(5)

    async def _ws_connect_and_run(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(_POLYGON_WS_URL) as ws:
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
                                    await self._dispatch(item)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                finally:
                    sender_task.cancel()

    async def _ws_sender(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            msg = await self._ws_send_queue.get()
            await ws.send_str(json.dumps(msg))

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
