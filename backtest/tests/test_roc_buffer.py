"""C4 unit tests — RocBuffer per-ticker rolling 5-minute ROC.

T2a: Full window (two polls 310s apart) → correct ROC and window_sec_actual.
T2b: Partial window (earliest poll 120s old) → uses oldest poll, does not block.
T2c: First appearance (no prior poll) → roc_5m = None.
T2d: Multi-ticker isolation → AAPL and TSLA buffers are independent.
T2e: Old polls pruned → buffer does not grow unbounded.
T2f: Nearest poll ≥ 5min used, not the oldest eligible poll.
"""
from __future__ import annotations

import pytest

from backtest.core.filters.roc_buffer import RocBuffer

_SEC = 1_000_000_000  # nanoseconds per second
_T0 = 1_700_000_000_000_000_000  # arbitrary base timestamp (ns)


# ---------------------------------------------------------------------------
# T2a — Full window
# ---------------------------------------------------------------------------

class TestT2aFullWindow:
    """Two polls 310s apart give the correct ROC and actual window."""

    def test_roc_value(self):
        buf = RocBuffer(window_sec=300.0)
        buf.update("AAPL", _T0, 10.0)
        buf.update("AAPL", _T0 + 310 * _SEC, 12.5)
        roc, _ = buf.compute("AAPL", _T0 + 310 * _SEC)
        assert roc == pytest.approx(2.5)

    def test_window_sec_actual(self):
        buf = RocBuffer(window_sec=300.0)
        buf.update("AAPL", _T0, 10.0)
        buf.update("AAPL", _T0 + 310 * _SEC, 12.5)
        _, window = buf.compute("AAPL", _T0 + 310 * _SEC)
        assert window == pytest.approx(310.0)


# ---------------------------------------------------------------------------
# T2b — Partial window
# ---------------------------------------------------------------------------

class TestT2bPartialWindow:
    """When no prior poll is ≥ 5min old, oldest available poll is the reference."""

    def test_roc_uses_oldest_poll(self):
        buf = RocBuffer(window_sec=300.0)
        buf.update("AAPL", _T0, 8.0)
        buf.update("AAPL", _T0 + 120 * _SEC, 10.5)
        roc, _ = buf.compute("AAPL", _T0 + 120 * _SEC)
        assert roc == pytest.approx(2.5)

    def test_window_sec_actual_reflects_partial_span(self):
        buf = RocBuffer(window_sec=300.0)
        buf.update("AAPL", _T0, 8.0)
        buf.update("AAPL", _T0 + 120 * _SEC, 10.5)
        _, window = buf.compute("AAPL", _T0 + 120 * _SEC)
        assert window == pytest.approx(120.0)


# ---------------------------------------------------------------------------
# T2c — First appearance
# ---------------------------------------------------------------------------

class TestT2cFirstAppearance:
    """No prior poll → roc_5m = None."""

    def test_none_after_first_update(self):
        buf = RocBuffer(window_sec=300.0)
        buf.update("TSLA", _T0, 15.0)
        roc, window = buf.compute("TSLA", _T0)
        assert roc is None
        assert window == 0.0

    def test_none_on_compute_without_update(self):
        buf = RocBuffer(window_sec=300.0)
        roc, window = buf.compute("NVDA", _T0)
        assert roc is None
        assert window == 0.0


# ---------------------------------------------------------------------------
# T2d — Multi-ticker isolation
# ---------------------------------------------------------------------------

class TestT2dMultiTickerIsolation:
    """AAPL and TSLA histories are fully independent."""

    def test_unknown_ticker_returns_none_when_other_ticker_has_history(self):
        buf = RocBuffer(window_sec=300.0)
        buf.update("AAPL", _T0, 10.0)
        buf.update("AAPL", _T0 + 310 * _SEC, 13.0)
        roc_tsla, _ = buf.compute("TSLA", _T0 + 310 * _SEC)
        assert roc_tsla is None

    def test_two_tickers_produce_independent_roc(self):
        buf = RocBuffer(window_sec=300.0)
        buf.update("AAPL", _T0, 10.0)
        buf.update("AAPL", _T0 + 310 * _SEC, 13.0)
        buf.update("TSLA", _T0, 50.0)
        buf.update("TSLA", _T0 + 310 * _SEC, 60.0)
        roc_aapl, _ = buf.compute("AAPL", _T0 + 310 * _SEC)
        roc_tsla, _ = buf.compute("TSLA", _T0 + 310 * _SEC)
        assert roc_aapl == pytest.approx(3.0)
        assert roc_tsla == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# T2e — Pruning
# ---------------------------------------------------------------------------

class TestT2ePruning:
    """Buffer prunes entries beyond the retention window."""

    def test_buffer_does_not_grow_unbounded(self):
        buf = RocBuffer(window_sec=300.0, retention_sec=600.0)
        # 200 polls at 10s intervals = 1990s of history; retention keeps ~600s worth
        for i in range(200):
            buf.update("AAPL", _T0 + i * 10 * _SEC, float(i))
        # With retention=600s and 10s spacing, at most ~61 entries should remain
        history_len = len(buf._history["AAPL"])
        assert history_len < 100, f"Expected < 100 entries after pruning, got {history_len}"

    def test_pruned_entries_do_not_affect_compute(self):
        """After pruning, compute still returns a valid ROC using retained entries."""
        buf = RocBuffer(window_sec=300.0, retention_sec=600.0)
        for i in range(100):
            buf.update("AAPL", _T0 + i * 10 * _SEC, float(i))
        # At t=990s, the poll from t=690s (300s ago) is the nearest ≥ 5min reference
        # pct at t=690s: i=69, so pct=69.0; current pct at t=990s: i=99, so pct=99.0
        roc, window = buf.compute("AAPL", _T0 + 99 * 10 * _SEC)
        assert roc is not None
        assert roc == pytest.approx(30.0)  # 99.0 - 69.0


# ---------------------------------------------------------------------------
# T2f — Nearest eligible poll is the reference
# ---------------------------------------------------------------------------

class TestT2fNearestPollUsed:
    """With multiple eligible prior polls, the most recent ≥ 5min one is used."""

    def test_nearest_not_oldest(self):
        buf = RocBuffer(window_sec=300.0)
        t_now = _T0 + 400 * _SEC
        buf.update("AAPL", _T0, 5.0)              # 400s before t_now — eligible
        buf.update("AAPL", _T0 + 90 * _SEC, 7.0)  # 310s before t_now — eligible (nearer)
        buf.update("AAPL", t_now, 12.0)            # current
        roc, window = buf.compute("AAPL", t_now)
        # T-310s (pct=7.0) is nearer than T-400s (pct=5.0) and still ≥ 300s old
        assert roc == pytest.approx(5.0)   # 12.0 - 7.0, not 12.0 - 5.0
        assert window == pytest.approx(310.0)
