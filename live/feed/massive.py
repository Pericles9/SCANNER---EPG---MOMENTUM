"""Massive (Polygon) REST price helper.

The single market-data pricing source for execution paths that have no live
WebSocket subscription — crash-recovery exits and the flatten paths (kill switch,
dead-man's switch, pending-close retry). **IBKR is execution-only; never use IBKR
market data for pricing or signal.** See CLAUDE.md locked decision.

Reuses the same Polygon API key (`POLYGON_API_KEY`) as the scanner and context
fetch — no new data dependency.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

# Single-ticker snapshot returns lastQuote (P=ask, p=bid) and lastTrade (p) in one call.
_SNAPSHOT_URL = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}"
_DEFAULT_TIMEOUT_S = 4.0


def _px(v) -> Optional[float]:
    """Coerce to a positive float price, else None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _parse_snapshot(body: dict) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Parse a Polygon single-ticker snapshot body into (bid, ask, last_trade).

    bid = lastQuote.p, ask = lastQuote.P, last = lastTrade.p. Any field None when
    absent or non-positive. Pure function — testable without HTTP.
    """
    tk = (body or {}).get("ticker") or {}
    lq = tk.get("lastQuote") or {}
    lt = tk.get("lastTrade") or {}
    return (_px(lq.get("p")), _px(lq.get("P")), _px(lt.get("p")))


async def fetch_mark(
    ticker: str,
    api_key: Optional[str] = None,
    *,
    session: Optional[aiohttp.ClientSession] = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (bid, ask, last_trade) from Massive REST. Any field None if unavailable.

    Never raises — returns (None, None, None) on missing key, HTTP error, or empty
    response. Prefer NBBO bid/ask (spread-aware) for a marketable limit; fall back
    to last trade.
    """
    key = api_key or os.getenv("POLYGON_API_KEY", "")
    if not key:
        log.warning("fetch_mark: no POLYGON_API_KEY available for %s", ticker)
        return (None, None, None)

    url = _SNAPSHOT_URL.format(ticker=ticker)
    params = {"apiKey": key}
    own_session = session is None
    sess = session or aiohttp.ClientSession()
    try:
        async with sess.get(url, params=params, timeout=aiohttp.ClientTimeout(total=timeout_s)) as resp:
            if resp.status != 200:
                log.warning("fetch_mark: %s HTTP %s", ticker, resp.status)
                return (None, None, None)
            body = await resp.json()
    except Exception:
        log.warning("fetch_mark: %s request failed", ticker, exc_info=True)
        return (None, None, None)
    finally:
        if own_session:
            await sess.close()

    return _parse_snapshot(body)


async def resolve_mark(
    signal_state,
    ticker: str,
    now_ns: int,
    api_key: Optional[str] = None,
    stale_s: float = 5.0,
) -> tuple[Optional[float], str, float]:
    """Best available mark for an open position, for display / unrealised P&L.

    Prefer the live WS-fed `signal_state.mark()`; when that has nothing fresh (stale
    WS feed) or there is no signal_state at all (a position orphaned out of the
    universe), fall back to a Massive REST fetch. Returns (price, source, age_s);
    source is `REST` for the fallback, `NONE` when even Massive has nothing. Never a
    silent 0.0.
    """
    if signal_state is not None:
        price, src, age = signal_state.mark(now_ns, stale_s)
        if price and price > 0:
            return price, src, age
    bid, ask, last = await fetch_mark(ticker, api_key)
    if last and last > 0:
        return last, "REST", 0.0
    if bid and ask and bid > 0 and ask > 0:
        return (bid + ask) / 2.0, "REST", 0.0
    return None, "NONE", 0.0
