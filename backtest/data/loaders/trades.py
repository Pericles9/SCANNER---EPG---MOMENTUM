"""
Trade data loader for the filtered event catalog.

Loads trades.parquet, computes lambda_ref (baseline arrival rate),
and provides session-filtered numpy arrays for downstream processing.

Key functions:
  compute_lambda_ref_per_event(ticker, event_date)
    Computes baseline arrival rate from T-3 to T-1 filtered trade data.
    Returns trades/sec averaged over the prior 3 trading days.
    Falls back to NaN if no prior data exists (caller should use global fallback).

  get_prev_close(ticker, event_date)
    Returns the most recent daily close price strictly before event_date,
    from the daily parquet files. Returns NaN if unavailable.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from data.schemas.mom_db import FILTERED_DIR, NS_PER_SECOND, DATA_ROOT

log = logging.getLogger(__name__)


# ── Event directory parsing ─────────────────────────────────────────────

_EVENT_RE = re.compile(
    r"^(?P<ticker>[A-Z0-9.p]+)_(?P<date>\d{4}-\d{2}-\d{2}|None)_(?P<mom>[\d.]+)$"
)


def parse_event_dir(name: str) -> Optional[dict]:
    """Parse event directory name into ticker, date, mom_pct."""
    m = _EVENT_RE.match(name)
    if m is None:
        return None
    return {
        "ticker": m.group("ticker"),
        "date": m.group("date") if m.group("date") != "None" else None,
        "mom_pct": float(m.group("mom")),
        "dir_name": name,
    }


def list_events(min_mom: float = 50.0, require_date: bool = True) -> list[dict]:
    """List all events in the filtered catalog matching criteria."""
    events = []
    for d in sorted(FILTERED_DIR.iterdir()):
        if not d.is_dir():
            continue
        info = parse_event_dir(d.name)
        if info is None:
            continue
        if require_date and info["date"] is None:
            continue
        if info["mom_pct"] < min_mom:
            continue
        # Require trades.parquet at minimum
        if not (d / "trades.parquet").exists():
            continue
        info["has_quotes"] = (d / "quotes.parquet").exists()
        events.append(info)
    return events


# ── Trade loading ───────────────────────────────────────────────────────


@dataclass
class TradeData:
    """Sorted trade arrays for a single event session."""
    timestamps: np.ndarray   # int64 nanoseconds
    prices: np.ndarray       # float64
    sizes: np.ndarray        # int64
    t_sec: np.ndarray        # float64 seconds from first trade
    n_trades: int
    ticker: str
    date: str
    mom_pct: float


def _et_offset_ns(date_str: str) -> int:
    """Approximate ET-to-UTC offset in nanoseconds for a date.

    Uses a simple DST rule: EDT (UTC-4) Mar second Sun through Nov first Sun,
    EST (UTC-5) otherwise.
    """
    year, month, day = (int(x) for x in date_str.split("-"))
    # Rough DST boundary: March 8-14 second Sun, Nov 1-7 first Sun
    if 3 < month < 11:
        return 4 * 3_600 * NS_PER_SECOND
    elif month == 3 and day >= 8:
        return 4 * 3_600 * NS_PER_SECOND
    elif month == 11 and day < 7:
        return 4 * 3_600 * NS_PER_SECOND
    return 5 * 3_600 * NS_PER_SECOND


def _session_ns_bounds(date_str: str) -> tuple[int, int]:
    """Return (session_start_ns, session_end_ns) for 4:00 AM – 8:00 PM ET.

    Timestamps are nanoseconds since Unix epoch.
    """
    from datetime import datetime, timezone

    year, month, day = (int(x) for x in date_str.split("-"))
    midnight_utc = int(
        datetime(year, month, day, tzinfo=timezone.utc).timestamp()
    ) * NS_PER_SECOND

    et_offset = _et_offset_ns(date_str)
    start_ns = midnight_utc + 4 * 3_600 * NS_PER_SECOND + et_offset  # 4am ET
    end_ns = midnight_utc + 20 * 3_600 * NS_PER_SECOND + et_offset   # 8pm ET
    return start_ns, end_ns


def load_trades(
    ticker: str,
    date: str,
    mom_pct: float,
    session_filter: bool = True,
) -> TradeData:
    """Load and sort trades for a single event, optionally filtering to session hours."""
    import pyarrow.parquet as pq

    dir_name = f"{ticker}_{date}_{mom_pct}"
    path = FILTERED_DIR / dir_name / "trades.parquet"
    if not path.exists():
        # Try matching with different float formatting
        candidates = list(FILTERED_DIR.glob(f"{ticker}_{date}_*"))
        if not candidates:
            raise FileNotFoundError(f"No trade data for {dir_name}")
        path = candidates[0] / "trades.parquet"

    table = pq.read_table(
        str(path),
        columns=["sip_timestamp", "price", "size"],
    )

    ts = table.column("sip_timestamp").to_numpy()
    prices = table.column("price").to_numpy()
    sizes = table.column("size").to_numpy()

    # Sort by timestamp
    order = np.argsort(ts)
    ts = ts[order]
    prices = prices[order].astype(np.float64)
    sizes = sizes[order].astype(np.int64)

    # Session filter: 4:00 AM – 8:00 PM ET on the event date
    if session_filter and date is not None:
        start_ns, end_ns = _session_ns_bounds(date)
        mask = (ts >= start_ns) & (ts <= end_ns)
        ts = ts[mask]
        prices = prices[mask]
        sizes = sizes[mask]

    # Seconds from first trade
    t_sec = (ts - ts[0]).astype(np.float64) / NS_PER_SECOND if len(ts) > 0 else np.array([], dtype=np.float64)

    return TradeData(
        timestamps=ts,
        prices=prices,
        sizes=sizes,
        t_sec=t_sec,
        n_trades=len(ts),
        ticker=ticker,
        date=date,
        mom_pct=mom_pct,
    )


def compute_lambda_ref(
    ticker: str,
    date: str,
    mom_pct: float,
) -> float:
    """Compute baseline arrival rate (trades/sec) for a session.

    lambda_ref = total trades / session duration in seconds.
    """
    td = load_trades(ticker, date, mom_pct, session_filter=True)
    if td.n_trades < 2:
        return 0.0
    duration_sec = td.t_sec[-1] - td.t_sec[0]
    if duration_sec <= 0:
        return 0.0
    return td.n_trades / duration_sec


# ── Per-event lambda_ref from T-3 to T-1 ──────────────────────────────

SESSION_SECONDS = 57_600  # 16-hour extended session (4 AM – 8 PM ET)


def compute_lambda_ref_per_event(ticker: str, event_date: str) -> float:
    """Compute per-event baseline arrival rate from T-3 to T-1 filtered trades.

    Scans the filtered event catalog for the same ticker on dates strictly
    before event_date, takes the 3 most recent, and computes:
        lambda_ref = total_trades / (n_days * 57600)

    Returns NaN if no prior data is found. Caller should fall back to the
    global constant from config/hawkes_params.json in that case.
    """
    import pyarrow.parquet as pq

    # Find all event dirs for this ticker with dates < event_date
    prior_dates: dict[str, list[Path]] = {}
    prefix = f"{ticker}_"
    for d in FILTERED_DIR.iterdir():
        if not d.is_dir() or not d.name.startswith(prefix):
            continue
        info = parse_event_dir(d.name)
        if info is None or info["date"] is None:
            continue
        if info["date"] < event_date:
            prior_dates.setdefault(info["date"], []).append(d)

    if not prior_dates:
        return float("nan")

    # Take the 3 most recent prior dates
    sorted_dates = sorted(prior_dates.keys(), reverse=True)[:3]

    total_trades = 0
    n_days = 0
    for dt in sorted_dates:
        # Sum trades across all events on this date (usually 1 per ticker-date)
        day_trades = 0
        for event_dir in prior_dates[dt]:
            trades_path = event_dir / "trades.parquet"
            if trades_path.exists():
                try:
                    table = pq.read_table(str(trades_path), columns=["sip_timestamp"])
                    day_trades += len(table)
                except Exception:
                    continue
        if day_trades > 0:
            total_trades += day_trades
            n_days += 1

    if n_days == 0:
        return float("nan")

    return total_trades / (n_days * SESSION_SECONDS)


# ── Prior close from daily parquet ─────────────────────────────────────


def load_bars_1m(ticker: str, date: str, mom_pct: float):
    """Reconstruct 1-minute OHLCV bars from ticks for one event session.

    Returns a pandas DataFrame with columns:
        ts (UTC datetime), open, high, low, close, volume.
    Empty bars are dropped (no row written for minutes with no trades).
    """
    import pandas as pd

    td = load_trades(ticker, date, mom_pct, session_filter=True)
    if td.n_trades == 0:
        return pd.DataFrame(
            columns=["ts", "open", "high", "low", "close", "volume"]
        )

    df = pd.DataFrame({
        "ts": pd.to_datetime(td.timestamps, unit="ns", utc=True),
        "price": td.prices.astype("float64"),
        "size": td.sizes.astype("int64"),
    }).set_index("ts")

    bars = df.resample("1min").agg(
        open=("price", "first"),
        high=("price", "max"),
        low=("price", "min"),
        close=("price", "last"),
        volume=("size", "sum"),
    )
    bars = bars.dropna(subset=["open"]).reset_index()
    return bars


def get_prev_close(ticker: str, event_date: str) -> float:
    """Get the most recent daily close price strictly before event_date.

    Reads from data/daily/{ticker}_daily.parquet. Returns NaN if the file
    does not exist or no prior date is found.
    """
    import pyarrow.parquet as pq
    from datetime import date as dt_date

    daily_path = DATA_ROOT / "daily" / f"{ticker}_daily.parquet"
    if not daily_path.exists():
        return float("nan")

    try:
        table = pq.read_table(str(daily_path), columns=["date", "close"])
        dates = table.column("date").to_pylist()
        closes = table.column("close").to_numpy()
        event_dt = dt_date.fromisoformat(event_date)

        best_idx = -1
        for i, d in enumerate(dates):
            # pyarrow may return datetime or date objects
            if hasattr(d, "date"):
                d = d.date()
            if d < event_dt and (best_idx < 0 or d > (dates[best_idx].date() if hasattr(dates[best_idx], "date") else dates[best_idx])):
                best_idx = i

        if best_idx < 0:
            return float("nan")
        return float(closes[best_idx])
    except Exception:
        return float("nan")
