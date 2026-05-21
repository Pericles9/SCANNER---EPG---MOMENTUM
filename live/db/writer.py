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
    """Double-buffer batch writer. Swaps hot/cold atomically on each flush."""

    def __init__(self) -> None:
        self._ticks: list = []
        self._quotes: list = []
        self._signal_events: list = []
        self._hawkes_refits: list = []

    def add_tick(self, record: tuple) -> None:
        self._ticks.append(record)

    def add_quote(self, record: tuple) -> None:
        self._quotes.append(record)

    def add_signal_event(self, record: tuple) -> None:
        self._signal_events.append(record)

    def add_hawkes_refit(self, record: tuple) -> None:
        self._hawkes_refits.append(record)

    async def run(self) -> None:
        interval = CFG.database.batch_flush_interval_s
        while True:
            await asyncio.sleep(interval)
            await self._flush()

    async def _flush(self) -> None:
        # Swap buffers atomically
        ticks, self._ticks = self._ticks, []
        quotes, self._quotes = self._quotes, []
        events, self._signal_events = self._signal_events, []
        refits, self._hawkes_refits = self._hawkes_refits, []

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
