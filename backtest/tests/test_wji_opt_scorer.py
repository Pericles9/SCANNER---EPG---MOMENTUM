"""
Unit tests for Phase WJI-OPT scorer (T2d).

Required cases:
  1. CVaR sign and tail-bucket size (floor(0.05 * n), min 1)
  2. capture_fraction is ratio-of-means, not mean-of-ratios (can diverge)
  3. Borda tie handling: tied values share average rank, no preference
  4. Hard filter rejection: each filter independently excludes configs
  5. compute_per_year groups trades correctly by year
  6. select_winner returns None when no config passes hard filters
  7. Borda tiebreaker: median_pct, then n_trades
  8. Empty trade list produces sentinel output (no crash)
  9. cvar5 with only 1 trade computes correctly
 10. capture_fraction is None when sum(available_move) == 0
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.phase_wji_opt.scorer import (
    compute_metrics,
    compute_per_year,
    apply_hard_filters,
    borda_rank,
    select_winner,
    _cvar5,
    _borda_rank_list,
)

# ── Default thresholds used in filter tests ────────────────────────────────
DEFAULT_THRESHOLDS = {
    "n_trades_floor": 5,
    "max_loss_floor_pct": -8.0,
    "cvar5_floor_pct": -4.0,
    "pf_floor": 1.0,
}


def _make_trades(pnl_list, avail_list=None, year="2024"):
    """Build a list of trade dicts."""
    if avail_list is None:
        avail_list = [max(p, 0.0) for p in pnl_list]
    assert len(pnl_list) == len(avail_list)
    return [
        {"pnl_pct": p, "available_move_pct": a, "year": year}
        for p, a in zip(pnl_list, avail_list)
    ]


# ══════════════════════════════════════════════════════════════════════
#  1. CVaR sign and tail-bucket size
# ══════════════════════════════════════════════════════════════════════

class TestCVaR:

    def test_cvar5_tail_bucket_size_20_trades(self):
        """floor(0.05 * 20) = 1 trade in tail."""
        pnl = list(range(1, 21))  # 1..20
        cvar, n = _cvar5(pnl)
        assert n == 1
        assert cvar == 1.0  # worst trade = 1

    def test_cvar5_tail_bucket_size_100_trades(self):
        """floor(0.05 * 100) = 5 trades in tail."""
        pnl = list(range(1, 101))
        cvar, n = _cvar5(pnl)
        assert n == 5
        assert cvar == pytest.approx(3.0)  # mean of 1,2,3,4,5

    def test_cvar5_minimum_bucket_1(self):
        """Even for 1 trade, bucket size = 1."""
        cvar, n = _cvar5([-5.0])
        assert n == 1
        assert cvar == -5.0

    def test_cvar5_negative_when_tail_is_losses(self):
        pnl = [1.0, 2.0, 3.0, -10.0, -8.0]
        cvar, _ = _cvar5(pnl)
        assert cvar < 0

    def test_cvar5_from_compute_metrics(self):
        """cvar5_pct in compute_metrics matches direct _cvar5 call."""
        trades = _make_trades([-10.0, -5.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
        m = compute_metrics(trades)
        expected_cvar, expected_n = _cvar5([t["pnl_pct"] for t in trades])
        assert m["cvar5_pct"] == pytest.approx(expected_cvar)
        assert m["n_cvar5_trades"] == expected_n

    def test_cvar5_empty_list_nan(self):
        cvar, n = _cvar5([])
        assert math.isnan(cvar)
        assert n == 0


# ══════════════════════════════════════════════════════════════════════
#  2. capture_fraction = ratio-of-means, not mean-of-ratios
# ══════════════════════════════════════════════════════════════════════

class TestCaptureFraction:

    def test_ratio_of_means_vs_mean_of_ratios_diverge(self):
        """
        Two trades: (pnl=1, avail=10) and (pnl=9, avail=10).
        ratio-of-means = (1+9)/(10+10) = 0.50
        mean-of-ratios = mean(1/10, 9/10) = mean(0.1, 0.9) = 0.50
        Equal here — use an asymmetric case to prove divergence.
        """
        # Asymmetric: (1, 2) and (9, 10)
        # ratio-of-means = (1+9)/(2+10) = 10/12 ≈ 0.833
        # mean-of-ratios = mean(1/2, 9/10) = mean(0.5, 0.9) = 0.70
        trades = _make_trades([1.0, 9.0], avail_list=[2.0, 10.0])
        m = compute_metrics(trades)
        assert m["capture_fraction"] == pytest.approx(10.0 / 12.0, rel=1e-6)
        # Verify it's NOT the mean-of-ratios
        mean_of_ratios = (1.0 / 2.0 + 9.0 / 10.0) / 2.0
        assert m["capture_fraction"] != pytest.approx(mean_of_ratios, rel=1e-6)

    def test_capture_fraction_none_when_avail_zero(self):
        trades = _make_trades([1.0, 2.0], avail_list=[0.0, 0.0])
        m = compute_metrics(trades)
        assert m["capture_fraction"] is None

    def test_capture_fraction_positive_when_net_positive(self):
        trades = _make_trades([2.0, 3.0], avail_list=[4.0, 5.0])
        m = compute_metrics(trades)
        assert m["capture_fraction"] == pytest.approx(5.0 / 9.0)

    def test_capture_fraction_uses_sum_not_per_trade(self):
        """Ensure denominator is sum(avail), not n_trades × mean(avail)."""
        trades = _make_trades([1.0], avail_list=[4.0])
        m = compute_metrics(trades)
        assert m["capture_fraction"] == pytest.approx(1.0 / 4.0)


# ══════════════════════════════════════════════════════════════════════
#  3. Borda tie handling
# ══════════════════════════════════════════════════════════════════════

class TestBordaTies:

    def test_tied_values_share_average_rank(self):
        """Three configs with same value → all get average of available points (0+1+2)/3 = 1.0."""
        values = [("A", 5.0), ("B", 5.0), ("C", 5.0)]
        scores = _borda_rank_list(values)
        assert scores["A"] == pytest.approx(1.0)
        assert scores["B"] == pytest.approx(1.0)
        assert scores["C"] == pytest.approx(1.0)

    def test_partial_tie(self):
        """A=3.0, B=3.0, C=1.0.  A and B tie for top (points: 2+1)/2=1.5 each, C gets 0."""
        values = [("A", 3.0), ("B", 3.0), ("C", 1.0)]
        scores = _borda_rank_list(values)
        assert scores["A"] == pytest.approx(1.5)
        assert scores["B"] == pytest.approx(1.5)
        assert scores["C"] == pytest.approx(0.0)

    def test_no_ties_strict_order(self):
        values = [("best", 10.0), ("mid", 5.0), ("worst", 1.0)]
        scores = _borda_rank_list(values)
        assert scores["best"] == 2.0
        assert scores["mid"] == 1.0
        assert scores["worst"] == 0.0

    def test_borda_rank_aggregate_with_ties(self):
        """When two configs are tied on all three axes, borda_rank preserves them both."""
        m = {
            "A": {"capture_fraction": 0.5, "ev": 1.0, "cvar5_pct": -2.0,
                  "median_pct": 0.5, "n_trades": 100},
            "B": {"capture_fraction": 0.5, "ev": 1.0, "cvar5_pct": -2.0,
                  "median_pct": 0.5, "n_trades": 100},
        }
        ranked = borda_rank(["A", "B"], m)
        assert set(ranked) == {"A", "B"}

    def test_borda_rank_empty_returns_empty(self):
        assert borda_rank([], {}) == []


# ══════════════════════════════════════════════════════════════════════
#  4. Hard filter rejection — each filter independently excludes
# ══════════════════════════════════════════════════════════════════════

class TestHardFilters:

    def _good_metrics(self):
        return {
            "n_trades": 300,
            "max_loss_pct": -3.0,
            "cvar5_pct": -2.0,
            "pf": 1.5,
            "capture_fraction": 0.4,
            "ev": 0.5,
            "median_pct": 0.3,
            "n_cvar5_trades": 15,
        }

    def test_passes_all_filters(self):
        m = {"ok": self._good_metrics()}
        survivors = apply_hard_filters(["ok"], m, DEFAULT_THRESHOLDS)
        assert survivors == ["ok"]

    def test_rejected_by_n_trades(self):
        m = {"low_n": {**self._good_metrics(), "n_trades": 4}}  # < floor 5
        survivors = apply_hard_filters(["low_n"], m, DEFAULT_THRESHOLDS)
        assert survivors == []

    def test_max_loss_not_a_hard_filter(self):
        """max_loss_pct is a reported diagnostic only — bad max_loss does not eliminate a config."""
        m = {"bad_loss": {**self._good_metrics(), "max_loss_pct": -50.0}}
        survivors = apply_hard_filters(["bad_loss"], m, DEFAULT_THRESHOLDS)
        assert survivors == ["bad_loss"]

    def test_rejected_by_cvar5(self):
        m = {"bad_cvar": {**self._good_metrics(), "cvar5_pct": -5.0}}
        survivors = apply_hard_filters(["bad_cvar"], m, DEFAULT_THRESHOLDS)
        assert survivors == []

    def test_rejected_by_pf(self):
        m = {"bad_pf": {**self._good_metrics(), "pf": 0.9}}
        survivors = apply_hard_filters(["bad_pf"], m, DEFAULT_THRESHOLDS)
        assert survivors == []

    def test_at_exact_floor_passes(self):
        m = {"boundary": {
            **self._good_metrics(),
            "n_trades": DEFAULT_THRESHOLDS["n_trades_floor"],
            "max_loss_pct": DEFAULT_THRESHOLDS["max_loss_floor_pct"],
            "cvar5_pct": DEFAULT_THRESHOLDS["cvar5_floor_pct"],
            "pf": DEFAULT_THRESHOLDS["pf_floor"],
        }}
        survivors = apply_hard_filters(["boundary"], m, DEFAULT_THRESHOLDS)
        assert survivors == ["boundary"]

    def test_multiple_configs_mixed(self):
        m = {
            "good": self._good_metrics(),
            "bad": {**self._good_metrics(), "n_trades": 1},
        }
        survivors = apply_hard_filters(["good", "bad"], m, DEFAULT_THRESHOLDS)
        assert survivors == ["good"]
        assert "bad" not in survivors


# ══════════════════════════════════════════════════════════════════════
#  5. compute_per_year groups correctly
# ══════════════════════════════════════════════════════════════════════

class TestPerYear:

    def test_groups_by_year(self):
        trades = (
            _make_trades([1.0, 2.0], year="2023") +
            _make_trades([3.0, 4.0, 5.0], year="2024")
        )
        by_year = compute_per_year(trades)
        assert set(by_year.keys()) == {"2023", "2024"}
        assert by_year["2023"]["n_trades"] == 2
        assert by_year["2024"]["n_trades"] == 3

    def test_per_year_metrics_match_direct(self):
        trades_2023 = _make_trades([1.0, -2.0, 3.0], year="2023")
        trades_2024 = _make_trades([0.5, -1.0], year="2024")
        combined = trades_2023 + trades_2024
        by_year = compute_per_year(combined)
        direct_2023 = compute_metrics(trades_2023)
        assert by_year["2023"]["ev"] == pytest.approx(direct_2023["ev"])
        assert by_year["2023"]["pf"] == pytest.approx(direct_2023["pf"])

    def test_single_year(self):
        trades = _make_trades([1.0, 2.0, 3.0], year="2022")
        by_year = compute_per_year(trades)
        assert list(by_year.keys()) == ["2022"]


# ══════════════════════════════════════════════════════════════════════
#  6. select_winner returns None when no survivor
# ══════════════════════════════════════════════════════════════════════

class TestSelectWinner:

    def test_returns_none_when_no_survivors(self):
        m = {
            "bad": {
                "n_trades": 1,  # < floor
                "max_loss_pct": -3.0, "cvar5_pct": -2.0, "pf": 1.5,
                "capture_fraction": 0.5, "ev": 1.0, "median_pct": 0.5, "n_cvar5_trades": 1,
            }
        }
        result = select_winner(m, DEFAULT_THRESHOLDS)
        assert result is None

    def test_returns_winner_when_one_passes(self):
        m = {
            "winner": {
                "n_trades": 300, "max_loss_pct": -3.0, "cvar5_pct": -2.0, "pf": 1.5,
                "capture_fraction": 0.5, "ev": 1.0, "median_pct": 0.5, "n_cvar5_trades": 15,
            }
        }
        result = select_winner(m, DEFAULT_THRESHOLDS)
        assert result == "winner"

    def test_returns_best_borda_winner(self):
        m = {
            "A": {
                "n_trades": 300, "max_loss_pct": -3.0, "cvar5_pct": -1.0, "pf": 1.5,
                "capture_fraction": 0.8, "ev": 2.0, "median_pct": 1.0, "n_cvar5_trades": 15,
            },
            "B": {
                "n_trades": 300, "max_loss_pct": -3.0, "cvar5_pct": -3.0, "pf": 1.3,
                "capture_fraction": 0.4, "ev": 1.0, "median_pct": 0.5, "n_cvar5_trades": 15,
            },
        }
        result = select_winner(m, DEFAULT_THRESHOLDS)
        assert result == "A"  # A dominates on all three Borda axes


# ══════════════════════════════════════════════════════════════════════
#  7. Borda tiebreaker: median_pct, then n_trades
# ══════════════════════════════════════════════════════════════════════

class TestBordaTiebreakers:

    def test_median_breaks_borda_tie(self):
        m = {
            "A": {
                "capture_fraction": 0.5, "ev": 1.0, "cvar5_pct": -2.0,
                "median_pct": 1.0, "n_trades": 100,
            },
            "B": {
                "capture_fraction": 0.5, "ev": 1.0, "cvar5_pct": -2.0,
                "median_pct": 0.5, "n_trades": 100,
            },
        }
        ranked = borda_rank(["A", "B"], m)
        assert ranked[0] == "A"  # higher median wins

    def test_n_trades_breaks_median_tie(self):
        m = {
            "A": {
                "capture_fraction": 0.5, "ev": 1.0, "cvar5_pct": -2.0,
                "median_pct": 0.5, "n_trades": 200,
            },
            "B": {
                "capture_fraction": 0.5, "ev": 1.0, "cvar5_pct": -2.0,
                "median_pct": 0.5, "n_trades": 100,
            },
        }
        ranked = borda_rank(["A", "B"], m)
        assert ranked[0] == "A"  # higher n_trades wins


# ══════════════════════════════════════════════════════════════════════
#  8. Empty trade list
# ══════════════════════════════════════════════════════════════════════

class TestEdgeCases:

    def test_empty_trades_no_crash(self):
        m = compute_metrics([])
        assert m["n_trades"] == 0
        assert m["capture_fraction"] is None
        assert m["ev"] is None
        assert m["cvar5_pct"] is None

    def test_single_trade(self):
        trades = _make_trades([2.0], avail_list=[5.0])
        m = compute_metrics(trades)
        assert m["n_trades"] == 1
        assert m["capture_fraction"] == pytest.approx(2.0 / 5.0)
        assert m["ev"] == pytest.approx(2.0)
        assert m["n_cvar5_trades"] == 1

    def test_all_winning_trades_pf_inf(self):
        trades = _make_trades([1.0, 2.0, 3.0])
        m = compute_metrics(trades)
        assert math.isinf(m["pf"])

    def test_all_losing_trades(self):
        trades = _make_trades([-1.0, -2.0, -3.0])
        m = compute_metrics(trades)
        assert m["pf"] == 0.0 or math.isnan(m["pf"])

    def test_per_year_empty(self):
        by_year = compute_per_year([])
        assert by_year == {}
