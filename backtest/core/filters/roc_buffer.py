"""Per-ticker rolling ROC buffer for scanner poll history.

Stores (timestamp_ns, pct_change) pairs per ticker. Workflow per poll:
  1. buf.update(ticker, ts_ns, pct_change)
  2. roc, window_sec = buf.compute(ticker, ts_ns)

roc is the difference between the current pct_change and the reference poll
(most recent prior poll at least window_sec old). Returns (None, 0.0) on first
appearance (no prior polls). Uses partial window when no poll is old enough.
"""
from __future__ import annotations

from collections import defaultdict


class RocBuffer:
    def __init__(self, window_sec: float = 300.0, retention_sec: float | None = None):
        self._window_ns = int(window_sec * 1_000_000_000)
        self._retention_ns = int(
            (retention_sec if retention_sec is not None else window_sec * 2)
            * 1_000_000_000
        )
        self._history: dict[str, list[tuple[int, float]]] = defaultdict(list)

    def update(self, ticker: str, ts_ns: int, pct_change: float) -> None:
        buf = self._history[ticker]
        buf.append((ts_ns, pct_change))
        cutoff = ts_ns - self._retention_ns
        i = 0
        while i < len(buf) - 1 and buf[i][0] < cutoff:
            i += 1
        if i:
            del buf[:i]

    def compute(self, ticker: str, ts_ns: int) -> tuple[float | None, float]:
        """Return (roc_5m, window_sec_actual).

        roc_5m is None on first appearance (no prior poll exists).
        window_sec_actual is the elapsed seconds between reference and current poll.
        """
        buf = self._history.get(ticker)
        if not buf:
            return None, 0.0

        cur_ts, cur_pct = buf[-1]
        prior = buf[:-1]

        if not prior:
            return None, 0.0

        lookback_cutoff = ts_ns - self._window_ns

        # Most recent prior poll at least window_sec old (nearest ≥ 5min)
        ref: tuple[int, float] | None = None
        for ts, pct in prior:
            if ts <= lookback_cutoff:
                ref = (ts, pct)

        if ref is None:
            # No prior poll old enough — use oldest available (partial window)
            ref = prior[0]

        ref_ts, ref_pct = ref
        window_sec_actual = (cur_ts - ref_ts) / 1_000_000_000
        return cur_pct - ref_pct, window_sec_actual
