"""Unit tests for core.exits.luld_scoring."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.exits.luld_scoring import (
    EventScore,
    FireEvent,
    HaltLabel,
    aggregate_scores,
    score_fires,
)

_NS = 1_000_000_000

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fire(ts_sec: float, spread_bps: float = 10.0, bid_sz: float = 100.0, mid: float = 10.0) -> FireEvent:
    return FireEvent(
        timestamp_ns=int(ts_sec * _NS),
        spread_bps=spread_bps,
        bid_size_shares=bid_sz,
        mid_price=mid,
    )


def _halt(start_sec: float, duration_sec: float = 120.0) -> HaltLabel:
    return HaltLabel(start_sec=start_sec, end_sec=start_sec + duration_sec)


# ---------------------------------------------------------------------------
# 1. Empty inputs
# ---------------------------------------------------------------------------

class TestEmptyInputs:
    def test_no_fires_no_halts(self):
        s = score_fires([], [])
        assert s.tp == 0
        assert s.fp == 0
        assert s.fn == 0
        assert s.recall == pytest.approx(0.0)
        assert s.composite == pytest.approx(0.0)

    def test_fires_no_halts(self):
        fires = [_fire(100.0), _fire(200.0)]
        s = score_fires(fires, [])
        assert s.tp == 0
        assert s.fp == 2
        assert s.fn == 0
        assert s.recall == pytest.approx(0.0)
        assert s.fp_rate == pytest.approx(1.0)

    def test_no_fires_with_halts(self):
        halts = [_halt(100.0), _halt(300.0)]
        s = score_fires([], halts)
        assert s.tp == 0
        assert s.fp == 0
        assert s.fn == 2
        assert s.recall == pytest.approx(0.0)
        assert s.fp_rate == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 2. TP / FP / FN classification
# ---------------------------------------------------------------------------

class TestClassification:
    def test_perfect_prediction(self):
        """Fire 10s before halt → TP."""
        fire = _fire(90.0)   # 90s
        halt = _halt(100.0)  # starts at 100s → lead = 10s ≤ 15s
        s = score_fires([fire], [halt])
        assert s.tp == 1
        assert s.fp == 0
        assert s.fn == 0
        assert s.recall == pytest.approx(1.0)
        assert s.precision == pytest.approx(1.0)
        assert s.fp_rate == pytest.approx(0.0)

    def test_fire_exactly_at_window_boundary(self):
        """Fire exactly pre_halt_window_sec before halt → TP (boundary inclusive)."""
        fire = _fire(85.0)   # 85s
        halt = _halt(100.0)  # lead = 15s exactly
        s = score_fires([fire], [halt], pre_halt_window_sec=15.0)
        assert s.tp == 1

    def test_fire_outside_window_is_fp(self):
        """Fire 20s before halt (> 15s window) → FP."""
        fire = _fire(80.0)
        halt = _halt(100.0)  # lead = 20s > 15s
        s = score_fires([fire], [halt])
        assert s.tp == 0
        assert s.fp == 1
        assert s.fn == 1

    def test_fire_after_halt_start_is_fp(self):
        """Fire occurring AFTER the halt started → FP (not a predictor)."""
        fire = _fire(105.0)  # after halt at 100.0
        halt = _halt(100.0)
        s = score_fires([fire], [halt])
        assert s.tp == 0
        assert s.fp == 1
        assert s.fn == 1

    def test_multiple_fires_one_halt(self):
        """Two fires within window; only one matches halt (closest precursor wins)."""
        fire_early = _fire(82.0)   # lead = 18s > 15s → ineligible
        fire_close = _fire(88.0)   # lead = 12s ≤ 15s → TP
        halt = _halt(100.0)
        s = score_fires([fire_early, fire_close], [halt])
        assert s.tp == 1
        assert s.fp == 1
        assert s.fn == 0

    def test_multiple_halts_multiple_fires(self):
        """Each halt matched to closest preceding fire; one fire per halt."""
        f1 = _fire(85.0)   # → matches halt at 100.0 (lead=15)
        f2 = _fire(185.0)  # → matches halt at 200.0 (lead=15)
        h1 = _halt(100.0)
        h2 = _halt(200.0)
        s = score_fires([f1, f2], [h1, h2])
        assert s.tp == 2
        assert s.fp == 0
        assert s.fn == 0
        assert s.recall == pytest.approx(1.0)

    def test_mixed_tp_fp_fn(self):
        """3 fires, 2 halts: one TP, one FP, one FN."""
        # halt1 at 100 — fire at 88 (lead=12s ≤ 15s) → TP
        # halt2 at 300 — no fire within 15s before → FN
        # fire at 200 — no halt within 15s after → FP
        fires = [_fire(88.0), _fire(200.0)]
        halts = [_halt(100.0), _halt(300.0)]
        s = score_fires(fires, halts)
        assert s.tp == 1
        assert s.fp == 1
        assert s.fn == 1
        assert s.recall == pytest.approx(0.5)
        assert s.precision == pytest.approx(0.5)
        assert s.fp_rate == pytest.approx(0.5)

    def test_extra_fires_in_halt_window_absorbed(self):
        """Phase LULD-V3c T3: repeated firing inside one halt window is one TP,
        and the extra in-window fires are NOT charged as FP (not false alarms)."""
        # Legacy halt (no onset) → window [85, 100]. Three fires all inside.
        fires = [_fire(86.0), _fire(92.0), _fire(99.0)]
        h = _halt(100.0)
        s = score_fires(fires, [h])
        assert s.tp == 1   # the halt is caught once
        assert s.fp == 0   # extra in-window fires absorbed, not FP
        assert s.fn == 0


# ---------------------------------------------------------------------------
# 2b. Limit-state-window anchor (Phase LULD-V3c T3 regression)
# ---------------------------------------------------------------------------

class TestLimitStateAnchor:
    """Regression for the V3b T6 0-TP bug: the scorer must match fires against the
    limit-state run [onset - pre_window, seg_end], not only the 15s before seg_end.

    Fixture mirrors CRBP 2024-01-26: limit-state onset ~60s before the freeze
    (seg_end). The exit fires during the approach, ~40-60s before seg_end — which
    the old seg_end-only anchor scored FP, producing recall 0 across the sample.
    """

    def _halt_with_onset(self, onset_sec, seg_end_sec, gap_sec=300.0):
        return HaltLabel(
            start_sec=seg_end_sec, end_sec=seg_end_sec + gap_sec,
            limit_state_start_sec=onset_sec,
        )

    def test_approach_fire_before_segend_is_tp(self):
        # onset=940, seg_end=1000 (60s limit-state run). Fire at 945 — 55s before
        # seg_end (old anchor: FP), but inside the limit-state window → TP.
        h = self._halt_with_onset(onset_sec=940.0, seg_end_sec=1000.0)
        f = _fire(945.0)
        s = score_fires([f], [h])
        assert s.tp == 1
        assert s.fp == 0
        assert s.fn == 0

    def test_old_anchor_would_have_missed(self):
        # The same fire, scored under a legacy label (no onset) → degrades to
        # [seg_end-15, seg_end] = [985, 1000]; fire at 945 is outside → FN/FP.
        legacy = _halt(1000.0)  # no limit_state_start_sec
        f = _fire(945.0)
        s = score_fires([f], [legacy])
        assert s.tp == 0
        assert s.fp == 1
        assert s.fn == 1

    def test_fire_within_pre_onset_lead_is_tp(self):
        # Fire 10s before the onset (in the [onset-15, onset] approach lead) → TP.
        h = self._halt_with_onset(onset_sec=940.0, seg_end_sec=1000.0)
        f = _fire(930.0)  # onset-10
        s = score_fires([f], [h], pre_halt_window_sec=15.0)
        assert s.tp == 1

    def test_fire_too_early_is_fp(self):
        # Fire 200s before onset → outside window → FP, halt FN.
        h = self._halt_with_onset(onset_sec=940.0, seg_end_sec=1000.0)
        f = _fire(740.0)
        s = score_fires([f], [h])
        assert s.tp == 0
        assert s.fp == 1
        assert s.fn == 1


# ---------------------------------------------------------------------------
# 3. Liquidity penalty
# ---------------------------------------------------------------------------

class TestLiquidityPenalty:
    def test_adequate_bid_size_no_penalty(self):
        """bid_size_shares >= shares_needed → liq_penalty = 0."""
        # mid=10.0, position_value=1000.0, shares_needed=100
        # bid_size=150 >= 100 → no penalty
        f = _fire(90.0, spread_bps=20.0, bid_sz=150.0, mid=10.0)
        h = _halt(100.0)
        s = score_fires([f], [h], position_value_usd=1000.0)
        assert s.mean_liq_penalty == pytest.approx(0.0)

    def test_insufficient_bid_size_penalised(self):
        """Phase LULD-V3c T4: penalty normalized to spread_bps / TARGET_SPREAD_BPS (100)."""
        # shares_needed = 1000 / 10.0 = 100; bid_size = 50 < 100 → penalised
        f = _fire(90.0, spread_bps=25.0, bid_sz=50.0, mid=10.0)
        h = _halt(100.0)
        s = score_fires([f], [h], position_value_usd=1000.0)
        assert s.mean_liq_penalty == pytest.approx(0.25)  # 25 / 100

    def test_penalty_capped_at_one(self):
        """Phase LULD-V3c T4: spread wider than TARGET clamps the penalty to 1.0."""
        f = _fire(90.0, spread_bps=350.0, bid_sz=50.0, mid=10.0)  # 350 bps, thin
        h = _halt(100.0)
        s = score_fires([f], [h], position_value_usd=1000.0)
        assert s.mean_liq_penalty == pytest.approx(1.0)

    def test_target_spread_bps_override(self):
        """target_spread_bps is configurable and rescales the penalty."""
        f = _fire(90.0, spread_bps=25.0, bid_sz=50.0, mid=10.0)
        h = _halt(100.0)
        s = score_fires([f], [h], position_value_usd=1000.0, target_spread_bps=50.0)
        assert s.mean_liq_penalty == pytest.approx(0.5)  # 25 / 50

    def test_mixed_liquidity(self):
        """Mean normalized penalty across fires with mixed liquidity."""
        f1 = _fire(85.0, spread_bps=10.0, bid_sz=200.0, mid=10.0)  # ok, no penalty
        f2 = _fire(90.0, spread_bps=30.0, bid_sz=50.0, mid=10.0)   # thin, 30/100=0.30
        h = _halt(100.0)
        s = score_fires([f1, f2], [h], position_value_usd=1000.0)
        assert s.mean_liq_penalty == pytest.approx(0.15)  # (0 + 0.30) / 2


# ---------------------------------------------------------------------------
# 4. Composite score
# ---------------------------------------------------------------------------

class TestCompositeScore:
    def test_perfect_signal_no_liq_penalty(self):
        """recall=1.0, fp_rate=0.0, mean_liq=0.0 → composite = w_recall."""
        f = _fire(90.0, spread_bps=10.0, bid_sz=200.0, mid=10.0)
        h = _halt(100.0)
        s = score_fires([f], [h], w_recall=3.0, w_fp=1.0, w_liq=1.0,
                        position_value_usd=1000.0)
        assert s.composite == pytest.approx(3.0 * 1.0 - 1.0 * 0.0 - 1.0 * 0.0)

    def test_all_fp_composite_negative(self):
        """All fires are FP → recall=0, fp_rate=1 → composite = -w_fp."""
        fires = [_fire(100.0), _fire(200.0)]
        s = score_fires(fires, [], w_recall=3.0, w_fp=1.0, w_liq=0.0)
        assert s.composite == pytest.approx(-1.0)

    def test_composite_with_liquidity_penalty(self):
        """Verify composite formula with normalized penalty (V3c T4)."""
        # recall=1.0, fp_rate=0.5 (1 TP, 1 FP), mean_liq=0.10 (10 bps / 100)
        f1 = _fire(88.0, spread_bps=10.0, bid_sz=50.0, mid=10.0)  # TP, 10/100=0.10
        f2 = _fire(150.0, spread_bps=10.0, bid_sz=50.0, mid=10.0)  # FP, 10/100=0.10
        h = _halt(100.0)
        s = score_fires([f1, f2], [h], w_recall=3.0, w_fp=1.0, w_liq=1.0,
                        position_value_usd=1000.0)
        # recall=1.0, fp_rate=0.5, mean_liq=0.10
        expected = 3.0 * 1.0 - 1.0 * 0.5 - 1.0 * 0.10
        assert s.composite == pytest.approx(expected, abs=1e-6)


# ---------------------------------------------------------------------------
# 5. HaltWindow passthrough (from luld_halt_detection)
# ---------------------------------------------------------------------------

class TestHaltWindowPassthrough:
    def test_accepts_halt_window_objects(self):
        """score_fires() accepts HaltWindow objects with .start and .end Timestamps."""
        import pandas as pd

        class _FakeHaltWindow:
            def __init__(self, start_sec, end_sec):
                self.start = pd.Timestamp(start_sec, unit="s")
                self.end = pd.Timestamp(end_sec, unit="s")
                self.reason = "luld"

        hw = _FakeHaltWindow(100.0, 220.0)
        f = _fire(88.0)
        s = score_fires([f], [hw])
        assert s.tp == 1


# ---------------------------------------------------------------------------
# 6. Aggregation
# ---------------------------------------------------------------------------

class TestAggregation:
    def test_aggregate_empty(self):
        a = aggregate_scores([])
        assert a.tp == 0
        assert a.n_fires == 0

    def test_aggregate_sums_counts(self):
        s1 = EventScore(n_fires=3, n_halts=2, tp=2, fp=1, fn=0,
                        recall=1.0, precision=0.667, fp_rate=0.333,
                        mean_liq_penalty=5.0, composite=2.0)
        s2 = EventScore(n_fires=1, n_halts=1, tp=0, fp=1, fn=1,
                        recall=0.0, precision=0.0, fp_rate=1.0,
                        mean_liq_penalty=20.0, composite=-2.0)
        a = aggregate_scores([s1, s2])
        assert a.n_fires == 4
        assert a.n_halts == 3
        assert a.tp == 2
        assert a.fp == 2
        assert a.fn == 1
        assert a.recall == pytest.approx(2 / 3)
        assert a.fp_rate == pytest.approx(2 / 4)
        # Weighted mean liq: (5*3 + 20*1) / 4 = 35/4 = 8.75
        assert a.mean_liq_penalty == pytest.approx(8.75)
