"""
Quote data loader for the filtered event catalog.

Loads quotes.parquet and provides session-filtered numpy arrays
for spread computation and quote-based OFI.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from data.schemas.mom_db import FILTERED_DIR, NS_PER_SECOND
from data.loaders.trades import _session_ns_bounds


@dataclass
class QuoteData:
    """Sorted quote arrays for a single event session."""
    timestamps: np.ndarray    # int64 nanoseconds
    bid_prices: np.ndarray    # float64
    ask_prices: np.ndarray    # float64
    bid_sizes: np.ndarray     # int64
    ask_sizes: np.ndarray     # int64
    t_sec: np.ndarray         # float64 seconds from first quote
    n_quotes: int
    ticker: str
    date: str
    mom_pct: float


def load_quotes(
    ticker: str,
    date: str,
    mom_pct: float,
    session_filter: bool = True,
) -> QuoteData:
    """Load and sort quotes for a single event, optionally filtering to session hours."""
    import pyarrow.parquet as pq

    dir_name = f"{ticker}_{date}_{mom_pct}"
    path = FILTERED_DIR / dir_name / "quotes.parquet"
    if not path.exists():
        candidates = list(FILTERED_DIR.glob(f"{ticker}_{date}_*"))
        if not candidates:
            raise FileNotFoundError(f"No quote data for {dir_name}")
        path = candidates[0] / "quotes.parquet"

    table = pq.read_table(
        str(path),
        columns=["sip_timestamp", "bid_price", "ask_price", "bid_size", "ask_size"],
    )

    ts = table.column("sip_timestamp").to_numpy()
    bid_p = table.column("bid_price").to_numpy().astype(np.float64)
    ask_p = table.column("ask_price").to_numpy().astype(np.float64)
    bid_s = table.column("bid_size").to_numpy().astype(np.int64)
    ask_s = table.column("ask_size").to_numpy().astype(np.int64)

    # Sort by timestamp
    order = np.argsort(ts)
    ts = ts[order]
    bid_p = bid_p[order]
    ask_p = ask_p[order]
    bid_s = bid_s[order]
    ask_s = ask_s[order]

    # Session filter
    if session_filter and date is not None:
        start_ns, end_ns = _session_ns_bounds(date)
        mask = (ts >= start_ns) & (ts <= end_ns)
        ts = ts[mask]
        bid_p = bid_p[mask]
        ask_p = ask_p[mask]
        bid_s = bid_s[mask]
        ask_s = ask_s[mask]

    t_sec = (ts - ts[0]).astype(np.float64) / NS_PER_SECOND if len(ts) > 0 else np.array([], dtype=np.float64)

    return QuoteData(
        timestamps=ts,
        bid_prices=bid_p,
        ask_prices=ask_p,
        bid_sizes=bid_s,
        ask_sizes=ask_s,
        t_sec=t_sec,
        n_quotes=len(ts),
        ticker=ticker,
        date=date,
        mom_pct=mom_pct,
    )


def compute_spread_bps(qd: QuoteData) -> np.ndarray:
    """Compute spread in basis points at each quote update.

    spread_bps = (ask - bid) / midpoint * 10_000
    """
    mid = (qd.bid_prices + qd.ask_prices) / 2.0
    valid = mid > 0
    spread = np.full(len(mid), np.nan)
    spread[valid] = (qd.ask_prices[valid] - qd.bid_prices[valid]) / mid[valid] * 10_000
    return spread
