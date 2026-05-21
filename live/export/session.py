"""End-of-session parquet export to filtered/ catalog."""
from __future__ import annotations

import logging
import os
import time
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from live.config import CFG
from live.db.pool import get_pool

log = logging.getLogger(__name__)

# Column schemas must match filtered/ catalog exactly (backtest pipeline reads these)
# NOTE: ticks schema does not include exchange or conditions — live system does not
# receive these fields from the Polygon WebSocket feed.
_TRADES_SCHEMA = pa.schema([
    ("sip_timestamp", pa.int64()),
    ("price", pa.float64()),
    ("size", pa.int64()),
])

_QUOTES_SCHEMA = pa.schema([
    ("sip_timestamp", pa.int64()),
    ("bid_price", pa.float64()),
    ("ask_price", pa.float64()),
    ("bid_size", pa.int64()),
    ("ask_size", pa.int64()),
])


async def export_session(
    ticker: str,
    session_date: date,
    intraday_pct: Optional[float] = None,
    theoretical_equity_end: Optional[float] = None,
    close_reason: str = "session_close",
) -> Optional[Path]:
    """Export session ticks/quotes to filtered/ catalog and update sessions close fields.

    Returns the export directory path, or None if export is disabled.
    NOTE: export may be skipped if the kill sequence fires mid-session — kill sequence
    prioritises position flatness and calls sys.exit(0) without waiting for export.
    """
    if not CFG.export.enabled:
        return None

    data_root = Path(os.environ.get("DATA_ROOT", "/data"))
    mom_pct = f"{intraday_pct:.2f}" if intraday_pct is not None else "0.00"
    dir_name = f"{ticker}_{session_date.isoformat()}_{mom_pct}"
    export_dir = data_root / "filtered" / dir_name
    export_dir.mkdir(parents=True, exist_ok=True)

    pool = get_pool()

    # Export ticks
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT sip_timestamp, price, size
            FROM ticks
            WHERE ticker=$1 AND session_date=$2
            ORDER BY sip_timestamp
            """,
            ticker, session_date,
        )

    if rows:
        trades_df = pd.DataFrame([dict(r) for r in rows])
        trades_table = pa.Table.from_pandas(trades_df, schema=_TRADES_SCHEMA, preserve_index=False)
        pq.write_table(trades_table, export_dir / "trades.parquet", compression="snappy")
        log.info("Exported %d ticks for %s/%s", len(rows), ticker, session_date)
    else:
        log.warning("No ticks to export for %s/%s", ticker, session_date)

    # Export quotes
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT sip_timestamp, bid_price, ask_price, bid_size, ask_size
            FROM quotes
            WHERE ticker=$1 AND session_date=$2
            ORDER BY sip_timestamp
            """,
            ticker, session_date,
        )

    if rows:
        quotes_df = pd.DataFrame([dict(r) for r in rows])
        quotes_table = pa.Table.from_pandas(quotes_df, schema=_QUOTES_SCHEMA, preserve_index=False)
        pq.write_table(quotes_table, export_dir / "quotes.parquet", compression="snappy")
        log.info("Exported %d quotes for %s/%s", len(rows), ticker, session_date)

    # Update sessions record with close time and theoretical equity
    closed_ns = time.time_ns()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE sessions
            SET closed_ns=$1,
                close_reason=$2,
                theoretical_equity_end=$3
            WHERE strategy_id=$4 AND ticker=$5 AND session_date=$6
            """,
            closed_ns,
            close_reason,
            theoretical_equity_end if theoretical_equity_end and theoretical_equity_end > 0 else None,
            CFG.strategy_id, ticker, session_date,
        )

    log.info("Session export complete: %s", export_dir)
    return export_dir
