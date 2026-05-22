"""Simple monotonic-time debounce for bot commands."""
from __future__ import annotations

import time
from collections import defaultdict

_last_call: dict[str, float] = defaultdict(float)
_DEBOUNCE_S = 2.0


def is_debounced(command: str) -> bool:
    """Return True (and skip) if this command was called within the debounce window."""
    now = time.monotonic()
    if now - _last_call[command] < _DEBOUNCE_S:
        return True
    _last_call[command] = now
    return False
