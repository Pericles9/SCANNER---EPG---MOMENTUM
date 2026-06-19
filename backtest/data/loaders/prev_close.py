"""Prior-close lookup for the screening-only runner (Phase S).

Priority chain:
  1. DuckDB `daily_bars` table (currently empty — placeholder for future ingest)
  2. data/daily/{TICKER}_daily.parquet — most recent close strictly before the event date
  3. trades.parquet of a prior event-day directory for the same ticker — last trade
     before 20:00 ET on the prior trading day
  4. Returns None if all sources fail.

Performance notes (SEB run — 6395+ sessions):
  - DuckDB usability is checked once and cached (_DUCKDB_HAS_DAILY_BARS flag).
  - Daily parquet is loaded once per ticker and cached in memory (_load_daily_parquet).
  - Prior-trades lookup uses a pre-built per-ticker index (_TICKER_EVENT_IDX) so
    it avoids re-scanning FILTERED_DIR (17K+ dirs) on every call.
  - Top-level get_prev_close should be wrapped with functools.lru_cache at the
    caller level for per-(ticker,date) deduplication.
"""
from __future__ import annotations

import functools
import logging
from datetime import date as dt_date
from pathlib import Path
from typing import Optional

import duckdb
import numpy as np
import pyarrow.parquet as pq

from data.schemas.mom_db import DATA_ROOT, FILTERED_DIR, NS_PER_SECOND

log = logging.getLogger(__name__)

_DAILY_DIR = DATA_ROOT / "daily"
_DUCKDB_PATH = DATA_ROOT / "duckdb" / "main.duckdb"
_RTH_CLOSE_HOUR_ET = 20  # 8 PM ET — use this as the cutoff for "prior session close"

# Set to False after the first check reveals daily_bars is absent/empty; avoids
# re-opening DuckDB on every subsequent call when the table is a placeholder.
# None = not yet checked.
_DUCKDB_HAS_DAILY_BARS: Optional[bool] = None

# Per-ticker index of prior event directories: ticker → [(ev_date, Path), ...]
# sorted DESCENDING by ev_date.  Built lazily on first call to the prior-trades
# fallback; avoids iterating 17K+ dirs on every get_prev_close call.
_TICKER_EVENT_IDX: Optional[dict[str, list]] = None


def _try_duckdb_daily_bars(ticker: str, date: str) -> Optional[float]:
    """Priority 1: DuckDB daily_bars table.

    Returns None if the table does not exist, is empty, the connection fails,
    or no row matches.  Checks usability once and short-circuits all future
    calls if the table is absent or empty.
    """
    global _DUCKDB_HAS_DAILY_BARS
    if _DUCKDB_HAS_DAILY_BARS is False:
        return None
    if not _DUCKDB_PATH.exists():
        _DUCKDB_HAS_DAILY_BARS = False
        return None
    try:
        con = duckdb.connect(str(_DUCKDB_PATH), read_only=True)
        try:
            tables = {r[0].lower() for r in con.execute("SHOW TABLES").fetchall()}
            if "daily_bars" not in tables:
                _DUCKDB_HAS_DAILY_BARS = False
                return None
            count_row = con.execute("SELECT COUNT(*) FROM daily_bars LIMIT 1").fetchone()
            if count_row is None or count_row[0] == 0:
                _DUCKDB_HAS_DAILY_BARS = False
                return None
            _DUCKDB_HAS_DAILY_BARS = True
            row = con.execute(
                "SELECT close FROM daily_bars "
                "WHERE ticker = ? AND session_date < ? "
                "ORDER BY session_date DESC LIMIT 1",
                [ticker, date],
            ).fetchone()
            if row is None:
                return None
            close = float(row[0])
            if close > 0 and np.isfinite(close):
                return close
            return None
        finally:
            con.close()
    except Exception as e:
        _DUCKDB_HAS_DAILY_BARS = False
        log.debug("daily_bars query failed for %s %s: %s", ticker, date, e)
        return None


@functools.lru_cache(maxsize=None)
def _load_daily_parquet(ticker: str) -> Optional[tuple]:
    """Load (dates_list, closes_array) from data/daily/{TICKER}_daily.parquet.

    Cached per ticker so the parquet is only read once regardless of how many
    date queries are made for the same ticker.  Returns None if the file is
    absent or unreadable.
    """
    path = _DAILY_DIR / f"{ticker}_daily.parquet"
    if not path.exists():
        return None
    try:
        table = pq.read_table(str(path), columns=["date", "close"])
        dates = table.column("date").to_pylist()
        closes = table.column("close").to_numpy()
        return (dates, closes)
    except Exception as e:
        log.debug("daily parquet load failed for %s: %s", ticker, e)
        return None


def _try_daily_parquet(ticker: str, date: str) -> Optional[float]:
    """Priority 2: data/daily/{TICKER}_daily.parquet — last close strictly before event date."""
    data = _load_daily_parquet(ticker)
    if data is None:
        return None
    dates, closes = data
    target = dt_date.fromisoformat(date)

    best_idx = -1
    best_date: Optional[dt_date] = None
    for i, d in enumerate(dates):
        if hasattr(d, "date"):
            d = d.date()
        if d < target and (best_date is None or d > best_date):
            best_date = d
            best_idx = i

    if best_idx < 0:
        return None
    close = float(closes[best_idx])
    if close > 0 and np.isfinite(close):
        return close
    return None


def _build_ticker_event_idx() -> dict[str, list]:
    """Build per-ticker sorted index of (ev_date, path) tuples from FILTERED_DIR.

    Scanned once; subsequent calls return the cached dict.  Sorted descending
    by date so _try_prior_trades_parquet can iterate most-recent first.
    """
    global _TICKER_EVENT_IDX
    if _TICKER_EVENT_IDX is not None:
        return _TICKER_EVENT_IDX

    from data.loaders.trades import parse_event_dir  # avoid circular at module level

    idx: dict[str, list] = {}
    if FILTERED_DIR.exists():
        for d in FILTERED_DIR.iterdir():
            if not d.is_dir():
                continue
            p = parse_event_dir(d.name)
            if p is None or p["date"] is None:
                continue
            try:
                ev_date = dt_date.fromisoformat(p["date"])
            except ValueError:
                continue
            tk = p["ticker"]
            if tk not in idx:
                idx[tk] = []
            idx[tk].append((ev_date, d))

    for tk in idx:
        idx[tk].sort(key=lambda x: x[0], reverse=True)

    log.debug("prev_close: indexed %d tickers from FILTERED_DIR", len(idx))
    _TICKER_EVENT_IDX = idx
    return idx


def _try_prior_trades_parquet(ticker: str, date: str) -> Optional[float]:
    """Priority 3: last trade in a prior event-day trades.parquet, before 20:00 ET on T-1.

    Uses a pre-built per-ticker index (one FILTERED_DIR scan for all tickers)
    instead of scanning the full directory on every call.
    """
    idx = _build_ticker_event_idx()
    target = dt_date.fromisoformat(date)

    for ev_date, ev_dir in idx.get(ticker, []):
        if ev_date >= target:
            continue  # index is descending; once we see ev_date < target, all rest are too
        trades_path = ev_dir / "trades.parquet"
        if not trades_path.exists():
            continue
        try:
            from datetime import datetime, timezone
            year, month, day = ev_date.year, ev_date.month, ev_date.day
            midnight_utc = int(
                datetime(year, month, day, tzinfo=timezone.utc).timestamp()
            ) * NS_PER_SECOND
            # Approximate ET offset: EDT (Mar..Nov) is 4h, EST is 5h
            if 3 < month < 11 or (month == 3 and day >= 8) or (month == 11 and day < 7):
                et_offset_ns = 4 * 3600 * NS_PER_SECOND
            else:
                et_offset_ns = 5 * 3600 * NS_PER_SECOND
            cutoff_ns = midnight_utc + 20 * 3600 * NS_PER_SECOND + et_offset_ns

            table = pq.read_table(
                str(trades_path),
                columns=["sip_timestamp", "price"],
            )
            ts = table.column("sip_timestamp").to_numpy()
            prices = table.column("price").to_numpy()
            mask = ts < cutoff_ns
            if not mask.any():
                continue
            order = np.argsort(ts[mask])
            last_price = float(prices[mask][order][-1])
            if last_price > 0 and np.isfinite(last_price):
                return last_price
        except Exception as e:
            log.debug("prior trades parquet read failed for %s: %s", ev_dir.name, e)
            continue

    return None


def get_prev_close(ticker: str, date: str) -> Optional[float]:
    """Return the prior trading-day close price, or None if no source resolves.

    Priority chain:
      1. DuckDB daily_bars table
      2. data/daily/{TICKER}_daily.parquet
      3. Last trade before 20:00 ET on the prior event-day in the filtered catalog
    """
    for fn in (_try_duckdb_daily_bars, _try_daily_parquet, _try_prior_trades_parquet):
        result = fn(ticker, date)
        if result is not None:
            return result
    return None
