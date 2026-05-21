"""Unit tests for core.exits.reentry.ReentrySignal."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.exits.reentry import ReentrySignal
from core.epg.gate import GateState

_NS = 1_000_000_000  # nanoseconds per second

THETA = 0.65
TAU_SEC = 4.0
TAU_NS = int(TAU_SEC * _NS)

# I_buy = 0.70 >= (1 - 0.65) = 0.35  → above buy threshold
_BUY_DOM = 0.70
_SELL_LOW = 0.30

# I_buy = 0.20 < 0.35  → below buy threshold
_BUY_WEAK = 0.20
_SELL_HIGH = 0.80


class TestReentrySignal:
    def _make(self, theta: float = THETA, tau_recovery_sec: float = TAU_SEC) -> ReentrySignal:
        return ReentrySignal(theta=theta, tau_recovery_sec=tau_recovery_sec)

    # ── constructor validation ────────────────────────────────────────────

    def test_theta_out_of_range_raises(self):
        with pytest.raises(ValueError):
            ReentrySignal(theta=0.0, tau_recovery_sec=4.0)
        with pytest.raises(ValueError):
            ReentrySignal(theta=1.0, tau_recovery_sec=4.0)

    def test_negative_tau_raises(self):
        with pytest.raises(ValueError):
            ReentrySignal(theta=0.65, tau_recovery_sec=0.0)
        with pytest.raises(ValueError):
            ReentrySignal(theta=0.65, tau_recovery_sec=-1.0)

    # ── 1. Timer starts correctly when buy dominance threshold crossed ────

    def test_timer_starts_on_threshold_cross(self):
        """First tick above buy threshold starts timer but does not fire."""
        sig = self._make()
        result = sig.update(0, _BUY_DOM, _SELL_LOW, GateState.PASS)
        assert result is False

    def test_no_fire_one_ns_before_tau(self):
        """Signal must not fire at tau_recovery - 1 ns."""
        sig = self._make()
        sig.update(0, _BUY_DOM, _SELL_LOW, GateState.PASS)
        result = sig.update(TAU_NS - 1, _BUY_DOM, _SELL_LOW, GateState.PASS)
        assert result is False

    # ── 2. Timer resets correctly on buy dominance drop ───────────────────

    def test_timer_resets_on_dominance_drop(self):
        """Timer clears when I_buy falls below threshold; subsequent ticks restart it."""
        sig = self._make()
        sig.update(0, _BUY_DOM, _SELL_LOW, GateState.PASS)         # timer starts at t=0
        sig.update(2 * _NS, _BUY_WEAK, _SELL_HIGH, GateState.PASS)  # drop → timer cleared
        # Dominance resumes at t=3s; timer now starts fresh at t=3s
        result_before_tau = sig.update(3 * _NS, _BUY_DOM, _SELL_LOW, GateState.PASS)
        assert result_before_tau is False, "timer restarted at +3s; tau not elapsed yet"
        # At t=3s + TAU_NS - 1: still not elapsed
        result_just_before = sig.update(3 * _NS + TAU_NS - 1, _BUY_DOM, _SELL_LOW, GateState.PASS)
        assert result_just_before is False

    def test_timer_resets_on_dominance_drop_then_fires_correctly(self):
        """After reset, timer fires tau_recovery seconds after the restart point."""
        sig = self._make()
        sig.update(0, _BUY_DOM, _SELL_LOW, GateState.PASS)          # timer starts at t=0
        sig.update(_NS, _BUY_WEAK, _SELL_HIGH, GateState.PASS)       # drop at t=1s
        sig.update(2 * _NS, _BUY_DOM, _SELL_LOW, GateState.PASS)     # restart at t=2s
        # At t=2s + TAU_NS: tau elapsed from t=2s → fire
        result = sig.update(2 * _NS + TAU_NS, _BUY_DOM, _SELL_LOW, GateState.PASS)
        assert result is True

    # ── 3. Re-entry does not fire when EPG state is not PASS ──────────────

    def test_no_fire_in_fail_state(self):
        """Timer fires only in GateState.PASS; FAIL resets and suppresses fire."""
        sig = self._make()
        sig.update(0, _BUY_DOM, _SELL_LOW, GateState.PASS)
        result = sig.update(TAU_NS + _NS, _BUY_DOM, _SELL_LOW, GateState.FAIL)
        assert result is False

    def test_no_fire_in_warmup_state(self):
        sig = self._make()
        sig.update(0, _BUY_DOM, _SELL_LOW, GateState.PASS)
        result = sig.update(TAU_NS + _NS, _BUY_DOM, _SELL_LOW, GateState.WARMUP)
        assert result is False

    def test_no_fire_in_inactive_state(self):
        sig = self._make()
        sig.update(0, _BUY_DOM, _SELL_LOW, GateState.PASS)
        result = sig.update(TAU_NS + _NS, _BUY_DOM, _SELL_LOW, GateState.INACTIVE)
        assert result is False

    def test_non_pass_resets_timer(self):
        """Non-PASS tick resets the timer so subsequent PASS ticks need a full tau."""
        sig = self._make()
        sig.update(0, _BUY_DOM, _SELL_LOW, GateState.PASS)           # timer starts
        sig.update(TAU_NS - _NS, _BUY_DOM, _SELL_LOW, GateState.FAIL) # timer reset
        # Immediately back to PASS at exactly TAU_NS: timer just restarted, should NOT fire
        result = sig.update(TAU_NS, _BUY_DOM, _SELL_LOW, GateState.PASS)
        assert result is False

    # ── 4. Re-entry fires correctly after tau_recovery elapsed ────────────

    def test_fires_at_exactly_tau(self):
        """Re-entry fires on the tick where elapsed >= tau_recovery."""
        sig = self._make()
        sig.update(0, _BUY_DOM, _SELL_LOW, GateState.PASS)   # timer starts at 0
        result = sig.update(TAU_NS, _BUY_DOM, _SELL_LOW, GateState.PASS)
        assert result is True

    def test_fires_past_tau(self):
        """Re-entry also fires when elapsed > tau_recovery (sparse ticks)."""
        sig = self._make()
        sig.update(0, _BUY_DOM, _SELL_LOW, GateState.PASS)
        result = sig.update(TAU_NS + 5 * _NS, _BUY_DOM, _SELL_LOW, GateState.PASS)
        assert result is True

    def test_does_not_fire_twice_without_reset(self):
        """After firing, timer is None; next tick must not fire again without new start."""
        sig = self._make()
        sig.update(0, _BUY_DOM, _SELL_LOW, GateState.PASS)
        first = sig.update(TAU_NS, _BUY_DOM, _SELL_LOW, GateState.PASS)
        assert first is True
        # Next tick: timer was cleared on fire; starts fresh now
        second = sig.update(TAU_NS + 1, _BUY_DOM, _SELL_LOW, GateState.PASS)
        assert second is False, "timer restarted after fire; tau not elapsed yet"

    # ── 5. Multiple re-entries fire correctly on repeated cycles ──────────

    def test_multiple_reentries_caller_resets_between(self):
        """Caller resets between fires; expect a new fire after each tau."""
        sig = self._make(tau_recovery_sec=2.0)
        tau_2ns = int(2.0 * _NS)

        fires_at = []
        t = 0
        while t <= 20 * _NS:
            fired = sig.update(t, _BUY_DOM, _SELL_LOW, GateState.PASS)
            if fired:
                fires_at.append(t)
                sig.reset()   # caller resets, as runner would after entering position
            t += _NS

        # With tau=2s and 1s ticks: fire at t=2s, reset, restart at t=3s, fire at t=5s, ...
        assert len(fires_at) >= 4, (
            f"expected ≥4 fires in 20s with tau=2s, 1s ticks; got {fires_at}"
        )

    def test_multiple_reentries_without_caller_reset(self):
        """Without caller reset, fires happen on consecutive ticks after tau, then restart."""
        sig = self._make(tau_recovery_sec=2.0)
        tau_2ns = int(2.0 * _NS)

        fire_count = 0
        for t_ns in range(0, 10 * _NS, _NS):
            if sig.update(t_ns, _BUY_DOM, _SELL_LOW, GateState.PASS):
                fire_count += 1
        # At least 2 fires in 10s with tau=2s (fires at t=2s and t=4s minimum)
        assert fire_count >= 2

    # ── 6. reset() prevents a pending fire ───────────────────────────────

    def test_reset_clears_timer(self):
        """reset() called before tau elapses prevents the fire."""
        sig = self._make()
        sig.update(0, _BUY_DOM, _SELL_LOW, GateState.PASS)  # timer starts
        sig.reset()
        # At exactly tau: timer was cleared, should not fire
        result = sig.update(TAU_NS, _BUY_DOM, _SELL_LOW, GateState.PASS)
        assert result is False

    # ── 7. Zero intensity edge case ───────────────────────────────────────

    def test_zero_total_intensity_does_not_fire(self):
        """When lam_buy + lam_sell == 0, I_buy = 0.0 < threshold; no fire."""
        sig = self._make()
        result = sig.update(TAU_NS + _NS, 0.0, 0.0, GateState.PASS)
        assert result is False
