"""Scanner monitor (Process 1): polls Polygon gainers, applies entry gate, feeds universe queue.

Entry gate:
    All quartiles Q1–Q4 admitted at all hours. One session per ticker per day.

Ticker eligibility:
    Only CS (common stock) tickers on XNYS or XNAS pass pre-quartile classification.
    Non-eligible tickers are stripped before quartile math runs so quartile ranks
    reflect the eligible population only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
import numpy as np

from live.config import CFG
from live.db.pool import get_pool
from live.feed import market_status
from live.scanner.context import compute_scanner_context
from live.ticker_classifier import classify_ticker

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

_last_poll_t: list[float] = [0.0]

# Most recent qualifying snapshot (all names passing >= gap_threshold). Bot /scanner reads this.
# Each entry: {"ticker": str, "pct_change": float, "quartile": int, "rank": int, "n": int}
last_scanner_snapshot: list[dict] = []


def get_last_poll_t() -> float:
    """Return monotonic time of the last completed scanner poll."""
    return _last_poll_t[0]


def get_last_scanner_snapshot() -> list[dict]:
    """Return the most recent qualifying snapshot (read-only — do not mutate)."""
    return last_scanner_snapshot
_GAINERS_URL = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/gainers"

# Peak trading windows (ET). Bounds: [start, end).
# Used when CFG.scanner.peak_hours_only is True.
_PEAK_WINDOWS: list[tuple[dtime, dtime]] = [
    (dtime(9, 30), dtime(11, 30)),
    (dtime(14, 0), dtime(16, 0)),
]


@dataclass
class ScannerContext:
    ticker: str
    pct_change: float
    scanner_rank: int
    scanner_n: int
    scanner_heat: float
    scanner_quartile: int
    snapshot_ns: int


@dataclass
class SnapshotRecord:
    snapshot_ns: int
    session_date: date
    n_qualifying: int
    heat_p75: Optional[float]
    snapshot_json: str


def get_now_et() -> datetime:
    """Return current datetime in US/Eastern timezone."""
    return datetime.now(_ET)


def is_peak_hours(dt: Optional[datetime] = None) -> bool:
    """Return True if dt (default: now ET) falls in a peak trading window.

    Peak windows: 09:30-11:30 ET and 14:00-16:00 ET. Start is inclusive, end is exclusive.
    """
    if dt is None:
        dt = get_now_et()
    t = dt.time()
    return any(start <= t < end for start, end in _PEAK_WINDOWS)


def _evaluate_entry_gate(quartile: int, dt: Optional[datetime] = None) -> bool:
    """Admit tickers according to config.

    When CFG.scanner.peak_hours_only is True: Q1+Q2 during peak hours only
    (09:30-11:30 and 14:00-16:00 ET); all else rejected.
    When False: all quartiles Q1–Q4 admitted at all hours.
    """
    if not CFG.scanner.peak_hours_only:
        return True
    if not is_peak_hours(dt):
        log.debug("[scanner] gate: off-peak — rejecting all tickers")
        return False
    if quartile not in (1, 2):
        log.debug("[scanner] gate: Q%d rejected (peak hours, but not Q1 or Q2)", quartile)
        return False
    return True


async def build_snapshot_context(
    http: aiohttp.ClientSession,
    api_key: str,
) -> tuple[list[ScannerContext], SnapshotRecord]:
    """Fetch Polygon gainers, classify tickers, compute Phase G v2 quartiles.

    1. Fetch /v2/snapshot/locale/us/markets/stocks/gainers.
    2. Filter by gap_threshold (pct_change >= threshold).
    3. Classify all candidates concurrently; keep only CS on XNYS/XNAS.
    4. Run Phase G v2 momentum-weighted quartile on the eligible set.

    Returns (contexts, snapshot_record). Contexts list is empty when no eligible tickers qualify.
    """
    params = {"apiKey": api_key}
    timeout = aiohttp.ClientTimeout(total=10)
    async with http.get(_GAINERS_URL, params=params, timeout=timeout) as resp:
        resp.raise_for_status()
        data = await resp.json()

    tickers_raw = data.get("tickers", [])
    gap_pct = CFG.scanner.gap_threshold * 100
    raw_qualifying = [
        {"ticker": t["ticker"], "pct_change": t.get("todaysChangePerc", 0.0), "raw": t}
        for t in tickers_raw
        if t.get("todaysChangePerc", 0.0) >= gap_pct
    ]

    snapshot_ns = time.time_ns()

    if not raw_qualifying:
        return [], SnapshotRecord(
            snapshot_ns=snapshot_ns,
            session_date=date.today(),
            n_qualifying=0,
            heat_p75=None,
            snapshot_json="[]",
        )

    metas = await asyncio.gather(
        *(classify_ticker(item["ticker"], api_key) for item in raw_qualifying)
    )
    eligible = [
        item for item, meta in zip(raw_qualifying, metas)
        if meta is not None and meta.is_eligible
    ]
    log.debug(
        "Scanner: %d raw qualifying -> %d eligible (CS+XNYS/XNAS)", len(raw_qualifying), len(eligible)
    )

    if not eligible:
        return [], SnapshotRecord(
            snapshot_ns=snapshot_ns,
            session_date=date.today(),
            n_qualifying=0,
            heat_p75=None,
            snapshot_json="[]",
        )

    enriched = compute_scanner_context(eligible)
    pct_values = np.array([item["pct_change"] for item in enriched])
    heat_p75 = float(np.percentile(pct_values, 75)) if len(pct_values) > 0 else None
    snapshot_json = json.dumps(
        [{k: v for k, v in item.items() if k != "raw"} for item in enriched]
    )

    contexts = [
        ScannerContext(
            ticker=item["ticker"],
            pct_change=item["pct_change"],
            scanner_rank=item["scanner_rank"],
            scanner_n=item["scanner_n"],
            scanner_heat=item["scanner_heat"],
            scanner_quartile=item["scanner_quartile"],
            snapshot_ns=snapshot_ns,
        )
        for item in enriched
    ]

    return contexts, SnapshotRecord(
        snapshot_ns=snapshot_ns,
        session_date=date.today(),
        n_qualifying=len(enriched),
        heat_p75=heat_p75,
        snapshot_json=snapshot_json,
    )


async def scanner_loop(
    universe_queue: asyncio.Queue,
    polygon_api_key: str,
    universe_mgr=None,
    closed_today: Optional[set] = None,
) -> None:
    """Main scanner polling loop (Process 1).

    Polls Polygon gainers every poll_interval_s seconds.
    All quartiles Q1–Q4 admitted. One session per ticker per day.
    """
    if closed_today is None:
        closed_today = set()

    async with aiohttp.ClientSession() as http:
        while True:
            try:
                await _poll_once(http, universe_queue, polygon_api_key, closed_today, universe_mgr)
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
    universe_mgr=None,
) -> None:
    # Refresh real market status (status every poll, holidays once per ET day)
    await market_status.refresh(http, api_key)

    contexts, record = await build_snapshot_context(http, api_key)
    _last_poll_t[0] = time.monotonic()

    # Refresh module-level snapshot for /scanner bot command
    global last_scanner_snapshot
    last_scanner_snapshot = [
        {
            "ticker": ctx.ticker,
            "pct_change": ctx.pct_change,
            "quartile": ctx.scanner_quartile,
            "rank": ctx.scanner_rank,
            "n": ctx.scanner_n,
        }
        for ctx in contexts
    ]

    if record.n_qualifying > 0:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO scanner_snapshots
                    (snapshot_ns, session_date, n_qualifying, heat_p75, snapshot_json)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                """,
                record.snapshot_ns,
                record.session_date,
                record.n_qualifying,
                record.heat_p75,
                record.snapshot_json,
            )

    # Scanner drop-off: remove tickers that fell out of the snapshot (no open position)
    if universe_mgr is not None and contexts:
        qualifying_tickers = {ctx.ticker for ctx in contexts}
        await universe_mgr.handle_snapshot_dropoffs(qualifying_tickers)

    now_et = get_now_et()
    for ctx in contexts:
        if not _evaluate_entry_gate(ctx.scanner_quartile, now_et):
            continue
        if ctx.ticker in closed_today:
            log.debug("[scanner] gate: %s already closed today", ctx.ticker)
            continue
        try:
            universe_queue.put_nowait((ctx.ticker, {
                "ticker": ctx.ticker,
                "pct_change": ctx.pct_change,
                "scanner_rank": ctx.scanner_rank,
                "scanner_n": ctx.scanner_n,
                "scanner_heat": ctx.scanner_heat,
                "scanner_quartile": ctx.scanner_quartile,
                "snapshot_ns": ctx.snapshot_ns,
            }))
            log.info(
                "[scanner] queued %s Q%d %.1f%%",
                ctx.ticker, ctx.scanner_quartile, ctx.pct_change,
            )
        except asyncio.QueueFull:
            log.warning("Universe queue full, dropped %s", ctx.ticker)

    log.info(
        "Scanner poll complete: %d qualifying, %d in snapshot, %d in closed_today",
        record.n_qualifying, len(contexts), len(closed_today),
    )


def mark_closed(closed_today: set[str], ticker: str) -> None:
    """Called by universe manager when a ticker session closes."""
    closed_today.add(ticker)
