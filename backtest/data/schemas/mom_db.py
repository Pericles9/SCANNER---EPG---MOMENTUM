"""
Schema definitions and paths for the mom_db filtered event catalog.
"""
from __future__ import annotations

from pathlib import Path

# ── Root paths ──────────────────────────────────────────────────────────

DATA_ROOT = Path(__file__).resolve().parents[4] / "data"
FILTERED_DIR = DATA_ROOT / "filtered"
CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

# ── Parquet column names ────────────────────────────────────────────────

TRADE_COLS = [
    "sip_timestamp",   # int64  nanoseconds since Unix epoch
    "price",           # float64
    "size",            # int64  shares
    "exchange",        # int64  exchange code
    "conditions",      # list<int64>
]

QUOTE_COLS = [
    "sip_timestamp",   # int64  nanoseconds since Unix epoch
    "bid_price",       # float64
    "ask_price",       # float64
    "bid_size",        # int64
    "ask_size",        # int64
]

# ── Session boundary helpers (nanosecond offsets from midnight UTC) ─────
# Session: 4:00 AM – 8:00 PM ET = 08:00 – 00:00 UTC (EST)
#          or 09:00 – 01:00 UTC (EDT)
# We use EDT (summer) as default; adjust per date if needed.

SESSION_START_ET_HOUR = 4   # 4:00 AM ET
SESSION_END_ET_HOUR = 20    # 8:00 PM ET

NS_PER_SECOND = 1_000_000_000
NS_PER_HOUR = 3_600 * NS_PER_SECOND
