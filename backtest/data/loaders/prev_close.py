"""Prior-close lookup for the screening-only runner (Phase S).

Priority chain:
  1. DuckDB `daily_bars` table (currently empty — placeholder for future ingest)
  2. data/daily/{TICKER}_daily.parquet — most recent close strictly before the event date
  3. trades.parquet of a prior event-day directory for the same ticker — last trade
     before 16:00 ET (RTH close) on the prior trading day
  4. Returns None if all sources fail.

Caller is responsible for caching: each lookup hits disk. Cache the result per
(ticker, date) at the runner level so the chain runs once per session.
"""
from __future__ import annotations

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
_RTH_CLOSE_HOUR_ET = 16  # 4 PM ET — RTH close cutoff for prior session price


def _try_duckdb_daily_bars(ticker: str, date: str) -> Optional[float]:
    """Priority 1: DuckDB daily_bars table.

    Returns None if the table does not exist, the connection fails, or no row matches.
    """
    if not _DUCKDB_PATH.exists():
        return None
    try:
        con = duckdb.connect(str(_DUCKDB_PATH), read_only=True)
        try:
            tables = {r[0].lower() for r in con.execute("SHOW TABLES").fetchall()}
            if "daily_bars" not in tables:
                return None
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
        log.debug(f"daily_bars query failed for {ticker} {date}: {e}")
        return None


def _try_daily_parquet(ticker: str, date: str) -> Optional[float]:
    """Priority 2: data/daily/{TICKER}_daily.parquet — last close strictly before event date."""
    path = _DAILY_DIR / f"{ticker}_daily.parquet"
    if not path.exists():
        return None
    try:
        table = pq.read_table(str(path), columns=["date", "close"])
        dates = table.column("date").to_pylist()
        closes = table.column("close").to_numpy()
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
    except Exception as e:
        log.debug(f"daily parquet read failed for {ticker} {date}: {e}")
        return None


def _try_prior_trades_parquet(ticker: str, date: str) -> Optional[float]:
    """Priority 3: last trade in a prior event-day trades.parquet, before 16:00 ET on T-1.

    Scans `filtered/{TICKER}_*/` for prior dates, takes the most recent, and returns the
    last trade price strictly before 16:00 ET (RTH close) on the prior session.
    """
    if not FILTERED_DIR.exists():
        return None
    target = dt_date.fromisoformat(date)
    prefix = f"{ticker}_"

    candidates: list[tuple[dt_date, Path]] = []
    for d in FILTERED_DIR.iterdir():
        if not d.is_dir() or not d.name.startswith(prefix):
            continue
        parts = d.name.split("_")
        if len(parts) < 2:
            continue
        try:
            ev_date = dt_date.fromisoformat(parts[1])
        except ValueError:
            continue
        if ev_date < target:
            candidates.append((ev_date, d))

    if not candidates:
        return None

    candidates.sort(reverse=True)

    # Compute 20:00 ET cutoff on the candidate's date in unix nanoseconds (UTC)
    for ev_date, ev_dir in candidates:
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
            cutoff_ns = midnight_utc + 16 * 3600 * NS_PER_SECOND + et_offset_ns

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
            log.debug(f"prior trades parquet read failed for {ev_dir.name}: {e}")
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
