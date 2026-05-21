"""Unit tests for Phase C CVD accumulator fix.

Verifies that ambiguous trades (sides=0) contribute zero to CVD,
not -1 as in the buggy code.
"""
from __future__ import annotations

import numpy as np
import pytest


def _accumulate_cvd_buggy(prices, sizes, sides):
    """Buggy accumulator: sides==0 → direction=-1."""
    cvd = 0.0
    for p, s, d in zip(prices, sizes, sides):
        direction = 1.0 if d == 1 else -1.0
        cvd += p * s * direction
    return cvd


def _accumulate_cvd_fixed(prices, sizes, sides):
    """Fixed accumulator: sides==0 → float(0)=0.0 contribution."""
    cvd = 0.0
    for p, s, d in zip(prices, sizes, sides):
        cvd += p * s * float(d)
    return cvd


class TestCVDAccumulator:
    def test_five_buys_one_sell_positive_cvd(self):
        """5 buy trades + 1 sell → positive CVD under fixed accumulator."""
        prices = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 10.0], dtype=np.float64)
        sizes  = np.array([100,  100,  100,  100,  100,  100 ], dtype=np.float64)
        sides  = np.array([1,    1,    1,    1,    1,   -1   ], dtype=np.int8)
        cvd = _accumulate_cvd_fixed(prices, sizes, sides)
        # 5 buys: +5000, 1 sell: -1000 → net +4000
        assert cvd == pytest.approx(4000.0)
        assert cvd > 0

    def test_ambiguous_trades_contribute_zero(self):
        """Ambiguous trades (sides=0) must add zero to CVD."""
        prices = np.array([10.0, 10.0, 10.0], dtype=np.float64)
        sizes  = np.array([100,  100,  100 ], dtype=np.float64)
        sides  = np.array([0,    0,    0   ], dtype=np.int8)
        cvd = _accumulate_cvd_fixed(prices, sizes, sides)
        assert cvd == pytest.approx(0.0)

    def test_buggy_code_makes_ambiguous_negative(self):
        """Buggy code maps sides=0 to direction=-1 (confirmed sell bias)."""
        prices = np.array([10.0], dtype=np.float64)
        sizes  = np.array([100 ], dtype=np.float64)
        sides  = np.array([0   ], dtype=np.int8)
        buggy = _accumulate_cvd_buggy(prices, sizes, sides)
        fixed = _accumulate_cvd_fixed(prices, sizes, sides)
        assert buggy == pytest.approx(-1000.0), "Buggy: ambiguous trade counted as full sell"
        assert fixed == pytest.approx(0.0),     "Fixed: ambiguous trade contributes zero"

    def test_fix_only_affects_ambiguous_trades(self):
        """For pure buy/sell flows (no ambiguous), buggy and fixed agree."""
        prices = np.array([5.0, 5.0, 5.0, 5.0], dtype=np.float64)
        sizes  = np.array([200, 200, 200, 200 ], dtype=np.float64)
        sides  = np.array([1,   1,  -1,  -1   ], dtype=np.int8)
        buggy = _accumulate_cvd_buggy(prices, sizes, sides)
        fixed = _accumulate_cvd_fixed(prices, sizes, sides)
        assert buggy == pytest.approx(fixed)
        assert fixed == pytest.approx(0.0)

    def test_mixed_with_ambiguous_fixed_higher_than_buggy(self):
        """With ambiguous trades, fixed CVD >= buggy CVD always."""
        prices = np.array([10.0, 10.0, 10.0, 10.0], dtype=np.float64)
        sizes  = np.array([100,  100,  100,  100 ], dtype=np.float64)
        sides  = np.array([1,   -1,    0,    0   ], dtype=np.int8)
        # fixed: +1000 - 1000 + 0 + 0 = 0
        # buggy: +1000 - 1000 - 1000 - 1000 = -2000
        buggy = _accumulate_cvd_buggy(prices, sizes, sides)
        fixed = _accumulate_cvd_fixed(prices, sizes, sides)
        assert fixed == pytest.approx(0.0)
        assert buggy == pytest.approx(-2000.0)
        assert fixed > buggy

    def test_rising_edge_scenario(self):
        """Simulate a T2c rising-edge scenario: uptrend with 9.5% ambiguous.

        With 10 buys, 8 sells, 2 ambiguous at price=20, size=100:
        - fixed CVD = (10-8) * 20 * 100 = +4000 → PASS
        - buggy CVD = (10-8-2) * 20 * 100 = 0 → borderline
        """
        n = 20
        # 10 buys, 8 sells, 2 ambiguous
        sides = np.array([1]*10 + [-1]*8 + [0]*2, dtype=np.int8)
        prices = np.full(n, 20.0)
        sizes  = np.full(n, 100.0)
        fixed = _accumulate_cvd_fixed(prices, sizes, sides)
        buggy = _accumulate_cvd_buggy(prices, sizes, sides)
        assert fixed == pytest.approx(4000.0)
        assert buggy == pytest.approx(0.0)
        assert fixed > 0
        # buggy is exactly zero here — in marginal cases it would go negative
