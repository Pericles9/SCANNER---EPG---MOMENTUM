"""NYSE/NASDAQ common stock filter for scanner context.

Classifies tickers as eligible (CS type + XNYS or XNAS primary exchange).
Lazy in-memory cache backed by the Polygon reference API.
Pre-populated from data/symbol-properties/symbol-properties-database.csv on import if present.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

_ELIGIBLE_TYPES = frozenset({"CS"})
_ELIGIBLE_EXCHANGES = frozenset({"XNYS", "XNAS"})
_TICKERS_URL = "https://api.polygon.io/v3/reference/tickers/{ticker}"
_CSV_PATH = (
    Path(__file__).parent.parent / "data" / "symbol-properties" / "symbol-properties-database.csv"
)


@dataclass(frozen=True)
class TickerMeta:
    ticker: str
    asset_type: str        # "CS", "ETF", "WARRANT", etc.
    primary_exchange: str  # "XNYS", "XNAS", "ARCX", etc.

    @property
    def is_eligible(self) -> bool:
        return self.asset_type in _ELIGIBLE_TYPES and self.primary_exchange in _ELIGIBLE_EXCHANGES


# Module-level cache. Absent key = not yet fetched. None value = fetch failed or not found.
_cache: dict[str, Optional[TickerMeta]] = {}
# In-flight dedup: if ticker is here, a lookup is already running.
_pending: set[str] = set()


def _load_csv() -> None:
    if not _CSV_PATH.exists():
        return
    try:
        count = 0
        with open(_CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ticker = (
                    row.get("ticker") or row.get("Ticker Symbol") or row.get("symbol", "")
                ).strip().upper()
                asset_type = (
                    row.get("type") or row.get("Type") or row.get("asset_class", "")
                ).strip().upper()
                exchange = (
                    row.get("primary_exchange") or row.get("Primary Exchange") or row.get("exchange", "")
                ).strip().upper()
                if ticker:
                    _cache[ticker] = TickerMeta(
                        ticker=ticker,
                        asset_type=asset_type,
                        primary_exchange=exchange,
                    )
                    count += 1
        log.info("Ticker classifier: pre-loaded %d tickers from %s", count, _CSV_PATH)
    except Exception:
        log.exception("Ticker classifier: CSV load failed — starting with empty cache")


_load_csv()


async def classify_ticker(ticker: str, api_key: str) -> Optional[TickerMeta]:
    """Return TickerMeta for ticker, or None if ineligible or unknown.

    Cache-first: returns immediately on hit. On miss, fetches Polygon reference API
    and caches the result (including None for 404/failures).

    Deduplicates concurrent requests: if a lookup is already in progress for this ticker,
    returns None immediately — the result will be in cache for the next poll cycle.
    """
    if ticker in _cache:
        return _cache[ticker]

    if ticker in _pending:
        log.debug("Ticker classifier: %s lookup already in progress, skipping this poll", ticker)
        return None

    _pending.add(ticker)
    try:
        meta = await _fetch_meta(ticker, api_key)
        _cache[ticker] = meta
        return meta
    except Exception:
        log.exception("Ticker classifier: lookup failed for %s — treating as ineligible", ticker)
        _cache[ticker] = None
        return None
    finally:
        _pending.discard(ticker)


async def _fetch_meta(ticker: str, api_key: str) -> Optional[TickerMeta]:
    url = _TICKERS_URL.format(ticker=ticker)
    params = {"apiKey": api_key}
    async with aiohttp.ClientSession() as http:
        async with http.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 404:
                log.debug("Ticker classifier: %s not found (404)", ticker)
                return None
            resp.raise_for_status()
            data = await resp.json()
    results = data.get("results")
    if not results:
        return None
    return TickerMeta(
        ticker=ticker,
        asset_type=results.get("type", ""),
        primary_exchange=results.get("primary_exchange", ""),
    )
