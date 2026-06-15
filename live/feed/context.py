"""TickerContext: per-ticker state bundle passed between feed components."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TickerContext:
    ticker: str
    queue: asyncio.Queue              # bounded — put_nowait drops on full
    signal_state: Any                 # LiveSignalState or VwapSignalState (duck-typed)
    task: asyncio.Task
    state_ready: asyncio.Event        # set after context fetch completes
    scanner_context: dict             # scanner fields at trigger time
    closed_today: bool = False
