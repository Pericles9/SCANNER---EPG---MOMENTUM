"""Async batch writer: 1s flush cycle using asyncpg COPY protocol."""
from __future__ import annotations

import asyncio
import logging

from live.config import CFG
from live.db.models import (
    HAWKES_REFITS_COLUMNS,
    QUOTES_COLUMNS,
    SIGNAL_EVENTS_COLUMNS,
    TICKS_COLUMNS,
)
from live.db.pool import get_pool

log = logging.getLogger(__name__)


class BatchWriter:
    """Batch writer that snapshots and clears shared hot-buffer lists on each flush.

    The hot-buffer lists are owned by main.py and shared by reference with the
    signal loops (which append to them). _flush() snapshots and clears each list
    in-place between await points, which is atomic in single-threaded asyncio.
    """

    def __init__(
        self,
        hot_ticks: list,
        hot_quotes: list,
        hot_signal_events: list,
        hot_hawkes_refits: list,
    ) -> None:
        self._ticks = hot_ticks
        self._quotes = hot_quotes
        self._signal_events = hot_signal_events
        self._hawkes_refits = hot_hawkes_refits

    async def run(self) -> None:
        interval = CFG.database.batch_flush_interval_s
        while True:
            await asyncio.sleep(interval)
            await self._flush()

    async def flush(self) -> None:
        """Flush remaining buffered records. Called on clean shutdown."""
        await self._flush()

    async def _flush(self) -> None:
        # Snapshot and clear in-place — no await between snapshot and clear,
        # so no signal_loop appends can interleave (single-threaded asyncio).
        ticks = self._ticks[:]
        del self._ticks[:]
        quotes = self._quotes[:]
        del self._quotes[:]
        events = self._signal_events[:]
        del self._signal_events[:]
        refits = self._hawkes_refits[:]
        del self._hawkes_refits[:]

        pool = get_pool()
        async with pool.acquire() as conn:
            if ticks:
                try:
                    await conn.copy_records_to_table(
                        "ticks", records=ticks, columns=TICKS_COLUMNS
                    )
                except Exception:
                    log.exception("BatchWriter: tick COPY failed (%d records)", len(ticks))

            if quotes:
                try:
                    await conn.copy_records_to_table(
                        "quotes", records=quotes, columns=QUOTES_COLUMNS
                    )
                except Exception:
                    log.exception("BatchWriter: quote COPY failed (%d records)", len(quotes))

            if events:
                try:
                    await conn.copy_records_to_table(
                        "signal_events", records=events, columns=SIGNAL_EVENTS_COLUMNS
                    )
                except Exception:
                    log.exception("BatchWriter: signal_events COPY failed (%d records)", len(events))

            if refits:
                try:
                    await conn.copy_records_to_table(
                        "hawkes_refits", records=refits, columns=HAWKES_REFITS_COLUMNS
                    )
                except Exception:
                    log.exception("BatchWriter: hawkes_refits COPY failed (%d records)", len(refits))
