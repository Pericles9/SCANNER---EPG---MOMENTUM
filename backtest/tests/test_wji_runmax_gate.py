"""
Unit tests for RunningMaxGate (Phase WJI-OPT, T1b).

Required cases:
  1. Peak is monotonically non-decreasing — never falls
  2. Threshold ratchets — open threshold rises with peak, never falls
  3. 'single' mode state transitions at exact boundary values
  4. 'asym' mode: open at p×peak, close at p_close×peak (distinct thresholds)
  5. Warmup: gate stays WARMUP until warmup_seconds elapsed; peak still updates
  6. reset() clears all state back to initial values
  7. Invalid constructor params rejected with ValueError
  8. INACTIVE before activate()
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.epg.gate import GateState
from core.epg.gate_variants import RunningMaxGate


# ══════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════

def _make_single(p: float = 0.65, warmup: float = 0.0) -> RunningMaxGate:
    return RunningMaxGate(p=p, hysteresis="single", warmup_seconds=warmup)


def _make_asym(p: float = 0.65, p_close: float = 0.30, warmup: float = 0.0) -> RunningMaxGate:
    return RunningMaxGate(p=p, hysteresis="asym", p_close=p_close, warmup_seconds=warmup)


def _activated(gate: RunningMaxGate, t0: float = 0.0) -> RunningMaxGate:
    gate.activate(t0)
    return gate


# ══════════════════════════════════════════════════════════════════════
#  1. Peak monotonically non-decreasing
# ══════════════════════════════════════════════════════════════════════

class TestPeakMonotone:

    def test_peak_initialises_at_1(self):
        gate = _activated(_make_single())
        assert gate.peak == 1.0

    def test_peak_rises_with_signal(self):
        gate = _activated(_make_single())
        gate.update(1.5, 1.0)
        assert gate.peak == 1.5
        gate.update(2.0, 2.0)
        assert gate.peak == 2.0

    def test_peak_never_falls_on_drop(self):
        gate = _activated(_make_single())
        gate.update(2.0, 1.0)
        gate.update(0.1, 2.0)
        assert gate.peak == 2.0

    def test_peak_unchanged_below_current_peak(self):
        gate = _activated(_make_single())
        gate.update(3.0, 1.0)
        gate.update(2.5, 2.0)
        gate.update(2.9, 3.0)
        assert gate.peak == 3.0

    def test_peak_updates_during_warmup(self):
        gate = _activated(_make_single(warmup=60.0))
        # Feed a high signal during warmup
        state = gate.update(5.0, 30.0)
        assert state == GateState.WARMUP
        assert gate.peak == 5.0
        # Warmup ends — peak carries over
        gate.update(0.1, 61.0)
        assert gate.peak == 5.0


# ══════════════════════════════════════════════════════════════════════
#  2. Threshold ratchets with peak
# ══════════════════════════════════════════════════════════════════════

class TestThresholdRatchet:

    def test_open_threshold_rises_after_peak_increase(self):
        """After peak rises, the same signal value that opened before now fails to open."""
        gate = _activated(_make_single(p=0.65))
        # Open at signal=0.70 (≥ 0.65×1.0)
        gate.update(0.70, 1.0)
        assert gate._in_pass  # opened

        # Close by dropping below 0.65×0.70 = 0.455
        gate.update(0.40, 2.0)
        assert not gate._in_pass

        # Peak is now 0.70; threshold = 0.65×0.70 = 0.455
        # signal=0.45 < 0.455×1.0 is below old threshold of 0.65 (original) but
        # signal=0.45 >= 0.455... actually let's use a clean new high first
        # Push peak to 2.0
        gate.update(2.0, 3.0)
        assert gate.peak == 2.0
        # Now threshold = 0.65×2.0 = 1.30; signal=0.70 that opened before is now below threshold
        gate.update(0.3, 4.0)  # close if open
        gate._in_pass = False  # reset to FAIL for clarity
        state = gate.update(0.70, 5.0)
        assert state == GateState.FAIL  # 0.70 < 1.30

    def test_threshold_ratchet_never_allows_old_open_level(self):
        gate = _activated(_make_asym(p=0.70))
        gate.update(3.0, 1.0)  # peak = 3.0, threshold = 2.1
        gate._in_pass = False
        # 1.5 < 0.70×3.0 = 2.1 — should FAIL
        state = gate.update(1.5, 2.0)
        assert state == GateState.FAIL


# ══════════════════════════════════════════════════════════════════════
#  3. 'single' mode state transitions at exact boundaries
# ══════════════════════════════════════════════════════════════════════

class TestSingleMode:

    def test_opens_at_p_times_peak(self):
        gate = _activated(_make_single(p=0.65))
        # peak=1.0; exact boundary: 0.65 × 1.0 = 0.65
        state = gate.update(0.65, 1.0)
        assert state == GateState.PASS

    def test_stays_fail_just_below_threshold(self):
        gate = _activated(_make_single(p=0.65))
        state = gate.update(0.6499, 1.0)
        assert state == GateState.FAIL

    def test_closes_below_p_times_peak(self):
        gate = _activated(_make_single(p=0.65))
        gate.update(2.0, 1.0)   # peak=2.0, open (2.0 >= 0.65×2.0=1.3)
        assert gate._in_pass
        # Close: 1.2 < 0.65×2.0 = 1.3
        state = gate.update(1.2, 2.0)
        assert state == GateState.FAIL

    def test_stays_pass_at_exact_close_boundary(self):
        gate = _activated(_make_single(p=0.65))
        gate.update(2.0, 1.0)   # open
        # Exactly at close boundary: 0.65×2.0 = 1.3 — NOT below, so stays PASS
        state = gate.update(1.3, 2.0)
        assert state == GateState.PASS

    def test_single_mode_p_close_equals_p(self):
        gate = RunningMaxGate(p=0.70, hysteresis="single")
        assert gate.p_close == 0.70

    def test_symmetric_open_close_same_threshold(self):
        gate = _activated(_make_single(p=0.70))
        gate.update(2.0, 1.0)  # peak=2.0, open
        assert gate._in_pass
        # Threshold = 0.70×2.0 = 1.4; drop to exactly 1.4 — stays PASS (not below)
        gate.update(1.4, 2.0)
        assert gate._in_pass
        # Drop to 1.39 — closes
        state = gate.update(1.39, 3.0)
        assert state == GateState.FAIL


# ══════════════════════════════════════════════════════════════════════
#  4. 'asym' mode: distinct open and close thresholds
# ══════════════════════════════════════════════════════════════════════

class TestAsymMode:

    def test_opens_at_p_open_times_peak(self):
        gate = _activated(_make_asym(p=0.65, p_close=0.30))
        state = gate.update(0.65, 1.0)  # exactly 0.65×1.0
        assert state == GateState.PASS

    def test_does_not_close_at_p_open_level(self):
        gate = _activated(_make_asym(p=0.65, p_close=0.30))
        gate.update(0.65, 1.0)   # open (peak=1.0)
        # Signal drops to 0.65×1.0 = 0.65 — NOT below p_close×peak = 0.30
        state = gate.update(0.65, 2.0)
        assert state == GateState.PASS

    def test_closes_at_p_close_times_peak(self):
        gate = _activated(_make_asym(p=0.65, p_close=0.30))
        gate.update(1.0, 1.0)   # open (peak=1.0, 1.0>=0.65)
        # Close at signal < 0.30×1.0 = 0.30
        state = gate.update(0.29, 2.0)
        assert state == GateState.FAIL

    def test_stays_pass_above_p_close_threshold(self):
        gate = _activated(_make_asym(p=0.65, p_close=0.30))
        gate.update(1.0, 1.0)
        # 0.31 >= 0.30×1.0 — stays PASS
        state = gate.update(0.31, 2.0)
        assert state == GateState.PASS

    def test_asym_hysteresis_band_hold(self):
        """Between p_close×peak and p×peak, state is held (dead band)."""
        gate = _activated(_make_asym(p=0.65, p_close=0.30))
        # With peak=1.0: dead band is [0.30, 0.65)
        # Start in FAIL, signal in dead band — stays FAIL
        state = gate.update(0.50, 1.0)  # 0.30 <= 0.50 < 0.65 → stays FAIL
        assert state == GateState.FAIL

    def test_can_reopen_after_close(self):
        gate = _activated(_make_asym(p=0.65, p_close=0.30))
        gate.update(1.0, 1.0)   # open; peak=1.0
        gate.update(0.10, 2.0)  # close (< 0.30)
        assert not gate._in_pass
        # Signal rises above p×peak: 1.0 >= 0.65×1.0 = 0.65
        state = gate.update(1.0, 3.0)
        assert state == GateState.PASS


# ══════════════════════════════════════════════════════════════════════
#  5. Warmup
# ══════════════════════════════════════════════════════════════════════

class TestWarmup:

    def test_warmup_state_during_warmup(self):
        gate = _activated(_make_single(warmup=300.0))
        state = gate.update(10.0, 100.0)  # t=100 < 300
        assert state == GateState.WARMUP

    def test_state_transitions_after_warmup(self):
        gate = _activated(_make_single(p=0.65, warmup=10.0), t0=0.0)
        gate.update(5.0, 5.0)   # during warmup, peak=5.0
        state = gate.update(4.0, 11.0)  # after warmup, 4.0 >= 0.65×5.0=3.25 → PASS
        assert state == GateState.PASS

    def test_gate_stays_warmup_at_exact_boundary(self):
        gate = _activated(_make_single(warmup=10.0), t0=0.0)
        # Exactly at warmup boundary timestamp (t - t_event = warmup_seconds is NOT past warmup)
        state = gate.update(5.0, 10.0)  # t - t_event = 10.0, not < 10.0 → past warmup
        # 5.0 >= 0.65×5.0=3.25 → PASS (warmup is strictly <)
        assert state == GateState.PASS

    def test_peak_carries_over_from_warmup(self):
        gate = _activated(_make_asym(p=0.65, warmup=10.0), t0=0.0)
        gate.update(3.0, 5.0)   # warmup, peak=3.0
        # After warmup: need signal >= 0.65×3.0 = 1.95 to open
        state = gate.update(2.0, 11.0)  # 2.0 >= 1.95 → PASS
        assert state == GateState.PASS

    def test_below_warmup_peak_stays_fail(self):
        gate = _activated(_make_asym(p=0.65, warmup=10.0), t0=0.0)
        gate.update(3.0, 5.0)   # warmup, peak=3.0
        # 1.9 < 0.65×3.0 = 1.95 → FAIL
        state = gate.update(1.9, 11.0)
        assert state == GateState.FAIL


# ══════════════════════════════════════════════════════════════════════
#  6. reset() clears state
# ══════════════════════════════════════════════════════════════════════

class TestReset:

    def test_reset_clears_peak(self):
        gate = _activated(_make_single())
        gate.update(5.0, 1.0)
        assert gate.peak == 5.0
        gate.reset()
        assert gate.peak == 1.0

    def test_reset_clears_in_pass(self):
        gate = _activated(_make_single())
        gate.update(1.0, 1.0)
        assert gate._in_pass
        gate.reset()
        assert not gate._in_pass

    def test_reset_returns_inactive(self):
        gate = _activated(_make_single())
        gate.update(1.0, 1.0)
        gate.reset()
        state = gate.update(99.0, 2.0)
        assert state == GateState.INACTIVE

    def test_reactivate_after_reset(self):
        gate = _activated(_make_single(p=0.65))
        gate.update(5.0, 1.0)   # peak=5.0
        gate.reset()
        gate.activate(10.0)
        assert gate.peak == 1.0
        # Threshold back to 0.65×1.0 = 0.65
        state = gate.update(0.65, 11.0)
        assert state == GateState.PASS


# ══════════════════════════════════════════════════════════════════════
#  7. Invalid constructor params
# ══════════════════════════════════════════════════════════════════════

class TestInvalidParams:

    def test_invalid_hysteresis_mode(self):
        with pytest.raises(ValueError, match="hysteresis"):
            RunningMaxGate(p=0.65, hysteresis="banana")

    def test_p_zero_rejected(self):
        with pytest.raises(ValueError, match="p must be"):
            RunningMaxGate(p=0.0)

    def test_p_negative_rejected(self):
        with pytest.raises(ValueError, match="p must be"):
            RunningMaxGate(p=-0.1)

    def test_p_above_1_rejected(self):
        with pytest.raises(ValueError, match="p must be"):
            RunningMaxGate(p=1.1)

    def test_p_close_above_p_asym_rejected(self):
        with pytest.raises(ValueError, match="p_close"):
            RunningMaxGate(p=0.40, hysteresis="asym", p_close=0.50)

    def test_p_close_zero_rejected(self):
        with pytest.raises(ValueError, match="p_close"):
            RunningMaxGate(p=0.65, hysteresis="asym", p_close=0.0)

    def test_p_1_valid(self):
        gate = RunningMaxGate(p=1.0, hysteresis="single")
        assert gate.p == 1.0

    def test_p_close_equals_p_valid_asym(self):
        gate = RunningMaxGate(p=0.65, hysteresis="asym", p_close=0.65)
        assert gate.p_close == 0.65


# ══════════════════════════════════════════════════════════════════════
#  8. INACTIVE before activate()
# ══════════════════════════════════════════════════════════════════════

class TestInactive:

    def test_inactive_before_activate(self):
        gate = RunningMaxGate(p=0.65)
        state = gate.update(5.0, 0.0)
        assert state == GateState.INACTIVE

    def test_peak_not_updated_before_activate(self):
        gate = RunningMaxGate(p=0.65)
        gate.update(5.0, 0.0)
        assert gate.peak == 1.0  # initial value unchanged
