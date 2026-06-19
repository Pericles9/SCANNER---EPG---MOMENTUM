"""C1 unit tests for entry_eligible() in rapid_entry.py."""
from __future__ import annotations

import numpy as np
import pytest

from backtest.core.filters.rapid_entry import entry_eligible, Q_THRESHOLD
from backtest.setup_filter import SetupFilterResult


def _make_result(q_tilde: list[float]) -> SetupFilterResult:
    arr = np.array(q_tilde, dtype=np.float64)
    n = len(arr)
    empty = np.array([], dtype=np.float64)
    return SetupFilterResult(
        passes=False,
        psi_passes=True,
        n_bars=n,
        first_qualify_bar=-1,
        last_fail_bar=-1,
        min_sustained_q=0.0,
        mean_q_tilde=float(np.mean(arr)) if n > 0 else 0.0,
        weakest_signal="none",
        range_scores=empty,
        vol_scores=empty,
        thin_scores=empty,
        body_scores=empty,
        q_raw=empty,
        q_tilde=arr,
    )


class TestEntryEligible:

    def test_returns_true_when_last_n_hold_bars_all_above_threshold(self):
        """T3d: n_hold=15 reproduces classic 15-bar sustain logic."""
        q = [0.70] * 20
        assert entry_eligible(_make_result(q), n_hold=15) is True

    def test_n_hold_1_true_when_last_bar_passes(self):
        """T3e: n_hold=1 returns True when only the last bar passes."""
        q = [0.50] * 10 + [0.70]
        assert entry_eligible(_make_result(q), n_hold=1) is True

    def test_n_hold_1_false_when_last_bar_fails(self):
        q = [0.70] * 10 + [0.60]
        assert entry_eligible(_make_result(q), n_hold=1) is False

    def test_n_hold_3_false_when_one_of_last_3_bars_below_threshold(self):
        """T3f: one bar below threshold in last 3 → False."""
        q = [0.70] * 8 + [0.60, 0.70, 0.70]
        assert entry_eligible(_make_result(q), n_hold=3) is False

    def test_n_hold_3_true_when_all_last_3_bars_above_threshold(self):
        q = [0.50] * 5 + [0.70, 0.72, 0.68]
        assert entry_eligible(_make_result(q), n_hold=3) is True

    def test_n_hold_3_false_at_exact_boundary_last_bar_below(self):
        q = [0.70, 0.70, Q_THRESHOLD - 0.001]
        assert entry_eligible(_make_result(q), n_hold=3) is False

    def test_n_hold_3_true_at_exact_threshold(self):
        q = [Q_THRESHOLD, Q_THRESHOLD, Q_THRESHOLD]
        assert entry_eligible(_make_result(q), n_hold=3) is True

    def test_insufficient_bars_returns_false(self):
        """Fewer bars than n_hold → False, no IndexError."""
        q = [0.70, 0.70]
        assert entry_eligible(_make_result(q), n_hold=3) is False

    def test_empty_q_tilde_returns_false(self):
        assert entry_eligible(_make_result([]), n_hold=3) is False

    def test_n_hold_15_false_when_one_bar_fails(self):
        """T3d inverse: classic 15-bar sustain with one fail → False."""
        q = [0.70] * 14 + [0.60]
        assert entry_eligible(_make_result(q), n_hold=15) is False

    def test_returns_python_bool(self):
        """Return type must be Python bool, not numpy bool_."""
        q = [0.70] * 5
        result = entry_eligible(_make_result(q), n_hold=3)
        assert type(result) is bool

    def test_default_n_hold_is_3(self):
        """Default n_hold=3: last 3 bars passing → True."""
        q = [0.50] * 5 + [0.70, 0.72, 0.68]
        assert entry_eligible(_make_result(q)) is True
