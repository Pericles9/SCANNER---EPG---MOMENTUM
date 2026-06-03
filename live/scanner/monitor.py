"""Process 1: Polygon gainers poller → universe queue."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date

import aiohttp
import numpy as np

from live.config import CFG
from live.db.pool import get_pool
from live.scanner.context import compute_scanner_context

log = logging.getLogger(__name__)

_GAINERS_URL = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/gainers"


async def scanner_monitor(
    universe_queue: asyncio.Queue,
    polygon_api_key: str,
) -> None:
    """Poll Polygon gainers every poll_interval_s seconds.

    Pushes (ticker, scanner_context_dict) to universe_queue for tickers
    that pass the quartile gate (trade_quartiles) and haven't closed today.
    """
    closed_today: set[str] = set()
    session_date = date.today()

    async with aiohttp.ClientSession() as http:
        while True:
            try:
                await _poll_once(http, universe_queue, polygon_api_key, closed_today, session_date)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Scanner poll error")
            await asyncio.sleep(CFG.scanner.poll_interval_s)


async def _poll_once(
    http: aiohttp.ClientSession,
    universe_queue: asyncio.Queue,
    api_key: str,
    closed_today: set[str],
    session_date: date,
) -> None:
    params = {"apiKey": api_key}
    timeout = aiohttp.ClientTimeout(total=10)
    async with http.get(_GAINERS_URL, params=params, timeout=timeout) as resp:
        resp.raise_for_status()
        data = await resp.json()

    tickers_raw = data.get("tickers", [])
    gap_threshold_pct = CFG.scanner.gap_threshold * 100  # config is decimal, API returns percent

    qualifying = []
    for t in tickers_raw:
        pct = t.get("todaysChangePerc", 0.0)
        if pct >= gap_threshold_pct:
            qualifying.append({"ticker": t["ticker"], "pct_change": pct, "raw": t})

    if not qualifying:
        return

    enriched = compute_scanner_context(qualifying)

    snapshot_ns = time.time_ns()
    pct_values = np.array([item["pct_change"] for item in enriched])
    heat_p75 = float(np.percentile(pct_values, 75)) if len(pct_values) > 0 else None
    snapshot = json.dumps([{k: v for k, v in item.items() if k != "raw"} for item in enriched])

    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO scanner_snapshots
                (snapshot_ns, session_date, n_qualifying, heat_p75, snapshot_json)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            """,
            snapshot_ns,
            session_date,
            len(enriched),
            heat_p75,
            snapshot,
        )

    for item in enriched:
        ticker = item["ticker"]
        quartile = item["scanner_quartile"]

        # Quartile gate removed (SlopeGate F_ss core swap) — all quartiles admitted.
        # NOTE: this module is an unused duplicate; the live scanner is live/scanner_monitor.py.
        if ticker in closed_today:
            log.debug("Scanner gate: %s already closed today", ticker)
            continue

        item["snapshot_ns"] = snapshot_ns
        try:
            universe_queue.put_nowait((ticker, item))
            log.info("Scanner: queued %s Q%d %.1f%%", ticker, quartile, item["pct_change"])
        except asyncio.QueueFull:
            log.warning("Universe queue full, dropped %s", ticker)


def mark_closed(closed_today: set[str], ticker: str) -> None:
    """Called by universe manager when a ticker session closes."""
    closed_today.add(ticker)
