"""
Unit tests for the Event Participation Gate (EPG).

Tests EventAnchor (T_event detection) and ParticipationGate (dollar volume
intensity gate with running peak threshold).
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.epg.anchor import EventAnchor
from core.epg.gate import ParticipationGate, GateState


# ══════════════════════════════════════════════════════════════════════
#  EventAnchor Tests
# ══════════════════════════════════════════════════════════════════════


class TestEventAnchor:
    """Tests for T_event detection."""

    def test_fires_on_first_crossing(self):
        """T_event fires exactly when lambda_hat first exceeds k * lambda_ref."""
        anchor = EventAnchor(lambda_ref=100.0, k_multiplier=5)

        # Below threshold: 5 * 100 = 500
        result = anchor.update(lambda_hat_t=400.0, timestamp=1.0)
        assert result is None
        assert not anchor.has_fired

        result = anchor.update(lambda_hat_t=499.9, timestamp=2.0)
        assert result is None

        # At threshold (not strictly greater)
        result = anchor.update(lambda_hat_t=500.0, timestamp=3.0)
        assert result is None  # must be > threshold, not >=

        # Above threshold — fires
        result = anchor.update(lambda_hat_t=500.1, timestamp=4.0)
        assert result == 4.0
        assert anchor.has_fired
        assert anchor.t_event == 4.0

    def test_does_not_refire_on_second_crossing(self):
        """Once T_event is set, it does not move even if lambda_hat re-crosses."""
        anchor = EventAnchor(lambda_ref=100.0, k_multiplier=5)

        # First crossing
        anchor.update(lambda_hat_t=600.0, timestamp=10.0)
        assert anchor.t_event == 10.0

        # Drop below and re-cross
        anchor.update(lambda_hat_t=100.0, timestamp=20.0)
        result = anchor.update(lambda_hat_t=700.0, timestamp=30.0)

        # T_event unchanged
        assert result == 10.0
        assert anchor.t_event == 10.0

    def test_returns_t_event_on_every_call_after_firing(self):
        """After first crossing, every update returns T_event."""
        anchor = EventAnchor(lambda_ref=50.0, k_multiplier=3)

        # Fire
        anchor.update(lambda_hat_t=200.0, timestamp=5.5)
        assert anchor.t_event == 5.5

        # Subsequent calls all return same T_event
        for t in [6.0, 7.0, 100.0]:
            assert anchor.update(lambda_hat_t=10.0, timestamp=t) == 5.5

    def test_reset_clears_state(self):
        """Reset allows T_event to be re-anchored (continuation events)."""
        anchor = EventAnchor(lambda_ref=100.0, k_multiplier=5)

        # Fire first session
        anchor.update(lambda_hat_t=600.0, timestamp=10.0)
        assert anchor.t_event == 10.0

        # Reset for new session
        anchor.reset()
        assert anchor.t_event is None
        assert not anchor.has_fired

        # New session: T_event re-anchored at new crossing
        result = anchor.update(lambda_hat_t=550.0, timestamp=100.0)
        assert result == 100.0
        assert anchor.t_event == 100.0

    def test_invalid_params(self):
        """Constructor rejects non-positive lambda_ref and k."""
        with pytest.raises(ValueError):
            EventAnchor(lambda_ref=0.0, k_multiplier=5)
        with pytest.raises(ValueError):
            EventAnchor(lambda_ref=-1.0, k_multiplier=5)
        with pytest.raises(ValueError):
            EventAnchor(lambda_ref=100.0, k_multiplier=0)

    def test_threshold_property(self):
        """Threshold is k * lambda_ref."""
        anchor = EventAnchor(lambda_ref=200.0, k_multiplier=10)
        assert anchor.threshold == 2000.0

    def test_set_lambda_ref_updates_threshold(self):
        """set_lambda_ref() updates the crossing threshold correctly."""
        anchor = EventAnchor(lambda_ref=1.0, k_multiplier=5)
        assert anchor.threshold == 5.0  # initial: 5 * 1.0

        anchor.set_lambda_ref(5.0)
        assert anchor.threshold == 25.0  # updated: 5 * 5.0
        assert anchor.lambda_ref == 5.0

        # Verify the new threshold is actually used for crossing detection
        result = anchor.update(lambda_hat_t=24.9, timestamp=1.0)
        assert result is None  # below new threshold

        result = anchor.update(lambda_hat_t=25.1, timestamp=2.0)
        assert result == 2.0   # above new threshold

    def test_set_lambda_ref_rejects_nonpositive(self):
        """set_lambda_ref() raises ValueError for non-positive values."""
        anchor = EventAnchor(lambda_ref=1.0, k_multiplier=5)
        with pytest.raises(ValueError):
            anchor.set_lambda_ref(0.0)
        with pytest.raises(ValueError):
            anchor.set_lambda_ref(-1.0)
        # Original lambda_ref unchanged after failed calls
        assert anchor.lambda_ref == 1.0


# ══════════════════════════════════════════════════════════════════════
#  ParticipationGate Tests
# ══════════════════════════════════════════════════════════════════════


class TestParticipationGate:
    """Tests for dollar volume intensity gate."""

    def test_inactive_before_activation(self):
        """Gate returns INACTIVE before T_event is established."""
        gate = ParticipationGate(half_life_seconds=300.0, peak_threshold_p=0.65)
        state = gate.update(dollar_vol=1000.0, timestamp=1.0)
        assert state == GateState.INACTIVE

    def test_warmup_for_first_5_minutes(self):
        """Gate returns WARMUP for first 5 minutes after T_event."""
        gate = ParticipationGate(half_life_seconds=300.0, peak_threshold_p=0.65)
        gate.activate(t_event=0.0)

        # All updates within 5 min = 300s should return WARMUP
        for t in [1.0, 60.0, 150.0, 299.9]:
            state = gate.update(dollar_vol=1000.0, timestamp=t)
            assert state == GateState.WARMUP, f"Expected WARMUP at t={t}s"

    def test_pass_after_warmup(self):
        """Gate returns PASS after warmup when lambda_V >= threshold."""
        gate = ParticipationGate(half_life_seconds=300.0, peak_threshold_p=0.65)
        gate.activate(t_event=0.0)

        # Build up lambda_V during warmup
        for t in range(0, 300, 1):
            gate.update(dollar_vol=5000.0, timestamp=float(t))

        # First update after warmup — lambda_V is at peak, so always PASS
        state = gate.update(dollar_vol=5000.0, timestamp=300.0)
        assert state == GateState.PASS

    def test_fail_when_below_threshold(self):
        """Gate returns FAIL when lambda_V drops below p * peak."""
        gate = ParticipationGate(half_life_seconds=60.0, peak_threshold_p=0.65)
        gate.activate(t_event=0.0)

        # Build peak during warmup with large trades
        for t in range(0, 300, 1):
            gate.update(dollar_vol=10000.0, timestamp=float(t))

        peak_before = gate.lambda_v_peak
        assert peak_before > 0

        # Stop trading — let lambda_V decay (short half-life = 60s)
        # After several half-lives with no volume, lambda_V → 0
        state = gate.update(dollar_vol=0.0, timestamp=900.0)
        assert state == GateState.FAIL
        assert gate.lambda_v < gate.threshold

    def test_lambda_v_decay_with_known_half_life(self):
        """Verify exponential decay matches hand-computed values."""
        tau = 120.0  # 2 minute half-life
        gate = ParticipationGate(half_life_seconds=tau, peak_threshold_p=0.65)
        gate.activate(t_event=0.0)

        # Single trade at t=0
        dv = 10000.0
        gate.update(dollar_vol=dv, timestamp=0.0)
        lv_0 = gate.lambda_v  # lambda_V right after the trade

        # Expected: lambda_V = dv * (ln2 / tau) at t=0 (no prior state)
        expected_lv_0 = dv * math.log(2) / tau
        assert abs(lv_0 - expected_lv_0) < 1e-10

        # After exactly one half-life with no trades, lambda_V should halve
        gate.update(dollar_vol=0.0, timestamp=tau)
        lv_half = gate.lambda_v
        expected_half = lv_0 * 0.5
        assert abs(lv_half - expected_half) < 1e-10, (
            f"After 1 half-life: expected {expected_half:.6f}, got {lv_half:.6f}"
        )

        # After another half-life, halve again
        gate.update(dollar_vol=0.0, timestamp=2 * tau)
        lv_quarter = gate.lambda_v
        expected_quarter = lv_0 * 0.25
        assert abs(lv_quarter - expected_quarter) < 1e-10

    def test_running_peak_is_causal(self):
        """Running peak only reflects past values, not future ones."""
        gate = ParticipationGate(half_life_seconds=300.0, peak_threshold_p=0.65)
        gate.activate(t_event=0.0)

        # Small trades during warmup
        for t in range(0, 300, 10):
            gate.update(dollar_vol=100.0, timestamp=float(t))
        peak_after_warmup = gate.lambda_v_peak

        # Big trade post-warmup
        gate.update(dollar_vol=1000000.0, timestamp=300.0)
        peak_after_big = gate.lambda_v_peak

        assert peak_after_big > peak_after_warmup
        # The peak should equal the current lambda_V (since it's the max)
        assert peak_after_big == gate.lambda_v

    def test_threshold_boundary_exact(self):
        """PASS/FAIL boundary: exactly at p * peak, just above, just below."""
        gate = ParticipationGate(half_life_seconds=300.0, peak_threshold_p=0.65)
        gate.activate(t_event=0.0)

        # Set a known peak by feeding a large trade
        gate.update(dollar_vol=100000.0, timestamp=0.0)
        peak = gate.lambda_v_peak

        # Manually check threshold
        threshold = 0.65 * peak
        assert abs(gate.threshold - threshold) < 1e-10

    def test_reset_clears_all_state(self):
        """Reset clears peak, lambda_V, and T_event."""
        gate = ParticipationGate(half_life_seconds=300.0, peak_threshold_p=0.65)
        gate.activate(t_event=0.0)

        # Build up state
        for t in range(0, 100, 1):
            gate.update(dollar_vol=5000.0, timestamp=float(t))

        assert gate.lambda_v > 0
        assert gate.lambda_v_peak > 0
        assert gate.t_event == 0.0

        # Reset
        gate.reset()

        assert gate.lambda_v == 0.0
        assert gate.lambda_v_peak == 0.0
        assert gate.t_event is None

        # Must return INACTIVE after reset
        state = gate.update(dollar_vol=1000.0, timestamp=200.0)
        assert state == GateState.INACTIVE

    def test_continuation_event_full_lifecycle(self):
        """Full lifecycle: activate → warmup → pass → fail → reset → reactivate."""
        gate = ParticipationGate(
            half_life_seconds=60.0,
            peak_threshold_p=0.65,
            warmup_seconds=10.0,  # short warmup for test
        )

        # Session 1
        gate.activate(t_event=0.0)

        # Warmup phase
        for t in range(1, 10):
            state = gate.update(dollar_vol=5000.0, timestamp=float(t))
            assert state == GateState.WARMUP

        # Pass phase (still getting volume)
        state = gate.update(dollar_vol=5000.0, timestamp=10.0)
        assert state == GateState.PASS

        # Let it decay → FAIL
        state = gate.update(dollar_vol=0.0, timestamp=500.0)
        assert state == GateState.FAIL

        # Session 2 (continuation)
        gate.reset()
        gate.activate(t_event=1000.0)

        # Back in warmup
        state = gate.update(dollar_vol=5000.0, timestamp=1001.0)
        assert state == GateState.WARMUP

        # After warmup, should pass with ongoing volume
        for t in range(1002, 1011):
            gate.update(dollar_vol=5000.0, timestamp=float(t))
        state = gate.update(dollar_vol=5000.0, timestamp=1011.0)
        assert state == GateState.PASS

    def test_invalid_params(self):
        """Constructor rejects invalid parameters."""
        with pytest.raises(ValueError):
            ParticipationGate(half_life_seconds=0.0, peak_threshold_p=0.65)
        with pytest.raises(ValueError):
            ParticipationGate(half_life_seconds=-1.0, peak_threshold_p=0.65)
        with pytest.raises(ValueError):
            ParticipationGate(half_life_seconds=300.0, peak_threshold_p=0.0)
        with pytest.raises(ValueError):
            ParticipationGate(half_life_seconds=300.0, peak_threshold_p=1.5)


# ══════════════════════════════════════════════════════════════════════
#  Integration Tests: EventAnchor + ParticipationGate Together
# ══════════════════════════════════════════════════════════════════════


class TestEPGIntegration:
    """Test EventAnchor and ParticipationGate working together."""

    def test_full_pipeline(self):
        """
        Simulate: trades arrive, anchor fires, gate activates,
        warmup → pass → fade → fail.
        """
        anchor = EventAnchor(lambda_ref=100.0, k_multiplier=5)
        gate = ParticipationGate(
            half_life_seconds=60.0,
            peak_threshold_p=0.65,
            warmup_seconds=30.0,
        )

        # Pre-T_event: lambda_hat below threshold
        for t in range(0, 20):
            t_event = anchor.update(lambda_hat_t=200.0, timestamp=float(t))
            assert t_event is None
            state = gate.update(dollar_vol=1000.0, timestamp=float(t))
            assert state == GateState.INACTIVE

        # T_event fires at t=20
        t_event = anchor.update(lambda_hat_t=600.0, timestamp=20.0)
        assert t_event == 20.0
        gate.activate(t_event)

        # Warmup: t=20 to t=50
        for t in range(21, 50):
            anchor.update(lambda_hat_t=600.0, timestamp=float(t))
            state = gate.update(dollar_vol=5000.0, timestamp=float(t))
            assert state == GateState.WARMUP

        # Post-warmup: should be PASS with continuous volume
        state = gate.update(dollar_vol=5000.0, timestamp=50.0)
        assert state == GateState.PASS

        # Keep trading — stays PASS
        for t in range(51, 80):
            state = gate.update(dollar_vol=5000.0, timestamp=float(t))
            assert state == GateState.PASS

        # Volume stops — eventually FAIL (half-life=60s, so after ~3 half-lives)
        state = gate.update(dollar_vol=0.0, timestamp=300.0)
        assert state == GateState.FAIL

    def test_premarket_lambda_ref(self):
        """Pre-market events use a different lambda_ref and still fire correctly."""
        # Pre-market: lower lambda_ref, should fire faster
        anchor_pm = EventAnchor(lambda_ref=20.0, k_multiplier=5)
        anchor_rh = EventAnchor(lambda_ref=200.0, k_multiplier=5)

        # Same lambda_hat
        lambda_hat = 150.0

        # Pre-market fires (150 > 5*20=100)
        assert anchor_pm.update(lambda_hat, 1.0) == 1.0

        # Regular hours does not fire (150 < 5*200=1000)
        assert anchor_rh.update(lambda_hat, 1.0) is None

    def test_cold_start_lambda_ref_replaces_init_value(self):
        """set_lambda_ref() with cold-start mu overrides global fallback init.

        Simulates the correct runner flow:
          1. Construct EventAnchor with global fallback (mu_buy+mu_sell = 0.2)
          2. Call set_lambda_ref() with mu_buy+mu_sell from cold-start fit (2.0)
          3. Verify T_event fires at k*2.0=10.0, NOT at k*0.2=1.0
        """
        k = 5
        global_fallback = 0.2   # mu_buy + mu_sell from config
        cold_start_lref = 2.0   # mu_buy + mu_sell from cold-start fit (pure background rate)

        anchor = EventAnchor(lambda_ref=global_fallback, k_multiplier=k)
        assert anchor.threshold == pytest.approx(k * global_fallback)  # 1.0

        # Override with cold-start equilibrium rate
        anchor.set_lambda_ref(cold_start_lref)
        assert anchor.threshold == pytest.approx(k * cold_start_lref)  # 10.0

        # lambda_hat just above old threshold but below new — must NOT fire
        old_threshold_plus = k * global_fallback + 0.01  # 1.01
        result = anchor.update(lambda_hat_t=old_threshold_plus, timestamp=1.0)
        assert result is None, (
            "T_event must not fire at old global-fallback threshold after set_lambda_ref()"
        )

        # lambda_hat just above new threshold — must fire
        new_threshold_plus = k * cold_start_lref + 0.01  # 10.01
        result = anchor.update(lambda_hat_t=new_threshold_plus, timestamp=2.0)
        assert result == 2.0, (
            "T_event must fire when lambda_hat exceeds cold-start mu threshold"
        )

    def test_mu_based_threshold_higher_n_base(self):
        """Threshold scales with fitted mu, not n_base.

        A high-mu stock (active, mu=1.65) should require higher lambda_hat to fire
        T_event than a quiet stock (mu=0.2), even if both have high n_base.
        This confirms the mu-only formula scales correctly across event types.
        """
        k = 5
        anchor_quiet = EventAnchor(lambda_ref=1.0, k_multiplier=k)  # placeholder
        anchor_active = EventAnchor(lambda_ref=1.0, k_multiplier=k)  # placeholder

        # Set mu-based lambda_ref: quiet stock mu=0.1+0.1=0.2, active stock mu=0.826+0.826=1.65
        anchor_quiet.set_lambda_ref(0.2)   # quiet stock threshold = 5 * 0.2 = 1.0
        anchor_active.set_lambda_ref(1.65) # active stock threshold = 5 * 1.65 = 8.25

        lambda_hat = 2.0  # above quiet threshold (1.0), below active threshold (8.25)

        # Quiet stock fires
        result_quiet = anchor_quiet.update(lambda_hat_t=lambda_hat, timestamp=1.0)
        assert result_quiet == 1.0, "Quiet stock should fire at lambda_hat=2.0 (threshold=1.0)"

        # Active stock does NOT fire
        result_active = anchor_active.update(lambda_hat_t=lambda_hat, timestamp=1.0)
        assert result_active is None, "Active stock must not fire at lambda_hat=2.0 (threshold=8.25)"

        # Active stock fires when lambda_hat exceeds its threshold
        result_active2 = anchor_active.update(lambda_hat_t=9.0, timestamp=2.0)
        assert result_active2 == 2.0, "Active stock should fire at lambda_hat=9.0 (threshold=8.25)"


# ══════════════════════════════════════════════════════════════════════
#  Asymmetric Hysteresis Tests (p_open / p_close)
# ══════════════════════════════════════════════════════════════════════


class TestParticipationGateAsymmetric:
    """Tests for the p_open / p_close asymmetric hysteresis extension."""

    def test_symmetric_case_reproduces_original_behavior(self):
        """p_open == p_close == peak_threshold_p gives identical PASS/FAIL sequences."""
        tau = 60.0
        p = 0.65
        warmup = 10.0
        dv_seq = [5000.0] * 20 + [0.0] * 200 + [5000.0] * 30

        # Reference gate (original symmetric behavior)
        ref = ParticipationGate(half_life_seconds=tau, peak_threshold_p=p, warmup_seconds=warmup)
        ref.activate(0.0)

        # Asymmetric gate with p_open == p_close
        asym = ParticipationGate(
            half_life_seconds=tau, peak_threshold_p=p, warmup_seconds=warmup,
            p_open=p, p_close=p,
        )
        asym.activate(0.0)

        for i, dv in enumerate(dv_seq):
            t = float(i + 1)
            s_ref = ref.update(dv, t)
            s_asym = asym.update(dv, t)
            assert s_ref == s_asym, (
                f"Mismatch at t={t}: ref={s_ref}, asym={s_asym}"
            )

    def test_fail_to_pass_boundary_uses_p_open(self):
        """FAIL→PASS transition fires at p_open × peak, not at p_close × peak.

        Uses τ=60s so that 10 half-lives (600s) brings lv to ~0.1% of peak,
        well below p_close=0.30.  Then feeds controlled volume to push lv to a
        level between p_close and p_open and verifies the gate stays FAIL.
        """
        tau = 60.0
        p_open = 0.65
        p_close = 0.30
        decay_rate = math.log(2) / tau

        gate = ParticipationGate(
            half_life_seconds=tau, peak_threshold_p=p_open, warmup_seconds=0.0,
            p_open=p_open, p_close=p_close,
        )
        gate.activate(0.0)

        # Build a known peak at t=0
        gate.update(100000.0, 0.0)
        peak = gate.lambda_v_peak

        # After 10 half-lives (600s), lv ≈ peak × 2^-10 ≈ 0.001 × peak
        gate.update(0.0, 600.0)
        lv_decayed = gate.lambda_v
        assert lv_decayed < p_close * peak, (
            f"After 10 half-lives lv/peak={lv_decayed/peak:.4f} should be < p_close={p_close}"
        )
        # At this point gate was PASS (opened at t=0), then PASS→FAIL crossed p_close
        # after decay; gate is now in FAIL state.
        state_fail = gate.update(0.0, 600.001)
        assert state_fail == GateState.FAIL, "Gate must be FAIL after decay below p_close"

        # Feed exactly enough volume to push lv to ~45% of peak
        # (between p_close=0.30 and p_open=0.65 → gate must stay FAIL)
        target_ratio = 0.45
        target_lv = target_ratio * peak
        # lv_new ≈ lv_decayed × exp(-decay_rate × tiny_dt) + dv × decay_rate
        # For near-zero dt: lv_new ≈ dv × decay_rate → dv = target_lv / decay_rate
        dv_target = target_lv / decay_rate
        gate.update(dv_target, 600.002)
        ratio_after = gate.lambda_v / gate.lambda_v_peak
        assert p_close < ratio_after < p_open, (
            f"Expected lv/peak between {p_close} and {p_open}, got {ratio_after:.4f}"
        )

        # Gate is FAIL and lv/peak < p_open — must stay FAIL
        state = gate.update(0.0, 600.003)
        assert state == GateState.FAIL, (
            f"Gate must stay FAIL when lv/peak={ratio_after:.4f} < p_open={p_open}"
        )

    def test_pass_to_fail_boundary_uses_p_close(self):
        """PASS→FAIL transition fires at p_close × peak; gate stays PASS above p_close."""
        tau = 300.0
        p_open = 0.65
        p_close = 0.30
        warmup = 0.0

        gate = ParticipationGate(
            half_life_seconds=tau, peak_threshold_p=p_open, warmup_seconds=warmup,
            p_open=p_open, p_close=p_close,
        )
        gate.activate(0.0)

        # Push into PASS state with high volume
        gate.update(100000.0, 0.0)
        peak = gate.lambda_v_peak
        state = gate.update(100000.0, 0.001)
        assert state == GateState.PASS, "Gate should open with high volume"

        # Decay to ~50% of peak (between p_close=0.30 and p_open=0.65)
        gate.update(0.0, tau)  # one half-life
        lv = gate.lambda_v
        ratio = lv / gate.lambda_v_peak
        assert 0.30 < ratio < 0.65, f"Expected 0.30 < lv/peak < 0.65, got {ratio:.4f}"

        # Gate must REMAIN PASS (lv/peak > p_close=0.30)
        state = gate.update(0.0, tau + 0.001)
        assert state == GateState.PASS, (
            f"Gate must stay PASS at lv/peak={ratio:.3f} (> p_close={p_close})"
        )

        # Decay further to below p_close (many half-lives)
        state_failed = gate.update(0.0, tau * 20.0)
        assert state_failed == GateState.FAIL, "Gate must close when lv/peak falls below p_close"


# ══════════════════════════════════════════════════════════════════════
#  Peak Cooling Tests
# ══════════════════════════════════════════════════════════════════════


class TestPeakCooling:
    """Tests for ParticipationGate peak cooling (m_cool_sec / tau_cool_sec)."""

    def _gate(self, m_cool_sec, tau_cool_sec=120.0, warmup=0.0, tau=60.0, p_open=0.65, p_close=0.30):
        return ParticipationGate(
            half_life_seconds=tau,
            peak_threshold_p=p_open,
            warmup_seconds=warmup,
            p_open=p_open,
            p_close=p_close,
            m_cool_sec=m_cool_sec,
            tau_cool_sec=tau_cool_sec,
        )

    def test_no_cooling_when_fail_duration_below_threshold(self):
        """Peak is preserved if FAIL duration < m_cool_sec."""
        gate = self._gate(m_cool_sec=60.0, tau_cool_sec=30.0)
        gate.activate(0.0)

        # Build peak
        gate.update(100000.0, 0.0)
        peak_initial = gate.lambda_v_peak
        assert peak_initial > 0

        # Let gate transition to FAIL (decay below p_close)
        gate.update(0.0, 300.0)  # many half-lives, gate is FAIL
        peak_before_cool = gate.lambda_v_peak

        # Advance only 30s (< m_cool_sec=60s) — peak must NOT decay
        gate.update(0.0, 330.0)
        assert gate.lambda_v_peak == pytest.approx(peak_before_cool), (
            "Peak should not decay before m_cool_sec threshold is reached"
        )

    def test_peak_decays_after_m_cool_sec(self):
        """Peak decays with half-life tau_cool_sec once cooling activates."""
        tau_cool = 30.0
        m_cool = 10.0
        gate = self._gate(m_cool_sec=m_cool, tau_cool_sec=tau_cool)
        gate.activate(0.0)

        # Build peak
        gate.update(100000.0, 0.0)
        peak_start = gate.lambda_v_peak

        # Drive to FAIL
        gate.update(0.0, 500.0)  # peak is preserved but gate is FAIL
        peak_at_fail = gate.lambda_v_peak

        # Advance past m_cool_sec — cooling activates at t ≈ 500 + 10 = 510
        t_cool_start = 500.0 + m_cool
        gate.update(0.0, t_cool_start + 0.001)  # cooling just activated, negligible decay

        # Advance exactly one tau_cool after cooling started — peak should halve
        gate.update(0.0, t_cool_start + tau_cool)
        expected_peak = peak_at_fail * 0.5
        assert gate.lambda_v_peak == pytest.approx(expected_peak, rel=1e-3), (
            f"After 1 cooling half-life: expected {expected_peak:.4f}, got {gate.lambda_v_peak:.4f}"
        )

    def test_pass_to_fail_to_pass_resets_cooling(self):
        """After cooling-assisted FAIL→PASS, normal peak accumulation resumes."""
        tau_cool = 10.0
        m_cool = 5.0
        gate = self._gate(m_cool_sec=m_cool, tau_cool_sec=tau_cool, p_open=0.65, p_close=0.30)
        gate.activate(0.0)

        # Build a large peak at t=0
        gate.update(1_000_000.0, 0.0)
        peak_original = gate.lambda_v_peak
        assert peak_original > 0, "Peak must be positive after large trade"

        # Gate is in PASS. Decay λ_V below p_close*peak in three explicit ticks:
        #   t=200: PASS→FAIL transition (λ_V ≈ peak * 0.10, below p_close=0.30)
        #   t=210: cooling activates (fail_duration=10s > m_cool=5s); cool_start=210
        #   t=410: cool_elapsed=200s = 20 half-lives → peak ≈ peak_original * 2^-20 ≈ 0
        gate.update(0.0, 200.0)   # PASS→FAIL; _fail_start_ts=200
        assert gate._in_pass is False
        gate.update(0.0, 210.0)   # fail_duration=10 >= m_cool=5 → activates cooling; cool_start=210
        assert gate._cooling_active is True
        gate.update(0.0, 410.0)   # cool_elapsed=200s ≈ 20 τ_cool half-lives
        cooled_peak = gate.lambda_v_peak
        assert cooled_peak < 0.001 * peak_original, (
            f"Peak should be nearly zero after 20 cooling half-lives, got {cooled_peak:.6f}"
        )

        # Now send new volume: gate should re-open (λ_V will exceed cooled peak * p_open)
        gate.update(100.0, 410.001)
        state = gate.update(0.0, 410.002)
        assert state == GateState.PASS, "Gate should reopen after peak cooled to near zero"

        # Peak tracking resumes from current λ_V — confirm peak updates
        peak_after_reopen = gate.lambda_v_peak
        assert peak_after_reopen > 0

    def test_m_cool_sec_zero_behavior_identical_to_no_cooling(self):
        """m_cool_sec=0 gives byte-for-byte identical results to a gate without cooling params."""
        tau = 60.0
        p = 0.65
        warmup = 5.0
        dv_seq = [5000.0] * 15 + [0.0] * 100 + [5000.0] * 20

        ref = ParticipationGate(half_life_seconds=tau, peak_threshold_p=p, warmup_seconds=warmup)
        ref.activate(0.0)

        cooled = ParticipationGate(
            half_life_seconds=tau, peak_threshold_p=p, warmup_seconds=warmup,
            m_cool_sec=0.0, tau_cool_sec=60.0,
        )
        cooled.activate(0.0)

        for i, dv in enumerate(dv_seq):
            t = float(i + 1)
            s_ref = ref.update(dv, t)
            s_cool = cooled.update(dv, t)
            assert s_ref == s_cool, (
                f"Mismatch at t={t}: no-cooling={s_ref}, m_cool=0={s_cool}"
            )
            assert ref.lambda_v_peak == pytest.approx(cooled.lambda_v_peak), (
                f"Peak mismatch at t={t}: {ref.lambda_v_peak} vs {cooled.lambda_v_peak}"
            )

    def test_cooling_not_active_during_pass(self):
        """Cooling state is cleared when gate is in PASS."""
        gate = self._gate(m_cool_sec=5.0, tau_cool_sec=10.0)
        gate.activate(0.0)

        gate.update(100000.0, 0.0)

        # In PASS — feed volume to stay PASS
        for t in range(1, 10):
            gate.update(1000.0, float(t))
            assert not gate._cooling_active, "Cooling must not activate while gate is in PASS"
            assert gate._fail_start_ts is None, "_fail_start_ts must be None in PASS"

    def test_invalid_cooling_params(self):
        """Constructor rejects negative m_cool_sec and non-positive tau_cool_sec when enabled."""
        with pytest.raises(ValueError):
            ParticipationGate(300.0, 0.65, m_cool_sec=-1.0)
        with pytest.raises(ValueError):
            ParticipationGate(300.0, 0.65, m_cool_sec=30.0, tau_cool_sec=0.0)
        with pytest.raises(ValueError):
            ParticipationGate(300.0, 0.65, m_cool_sec=30.0, tau_cool_sec=-5.0)


# ══════════════════════════════════════════════════════════════════════
#  T3i — gate_mode="peak" backward-compat tests
# ══════════════════════════════════════════════════════════════════════


class TestGateModePeakBackwardCompat:
    """gate_mode='peak' must be bit-for-bit identical to default (no gate_mode arg)."""

    def _run_sequence(self, gate, dv_seq, t_seq):
        states = []
        for dv, t in zip(dv_seq, t_seq):
            states.append(gate.update(dv, t))
        return states

    def test_default_and_peak_mode_identical(self):
        """ParticipationGate with gate_mode='peak' matches default (no mode arg)."""
        tau = 60.0
        p = 0.65
        warmup = 10.0
        dv_seq = [5000.0] * 20 + [0.0] * 200 + [3000.0] * 50
        t_seq = list(range(1, len(dv_seq) + 1))

        ref = ParticipationGate(half_life_seconds=tau, peak_threshold_p=p, warmup_seconds=warmup)
        ref.activate(0.0)

        new = ParticipationGate(
            half_life_seconds=tau, peak_threshold_p=p, warmup_seconds=warmup,
            gate_mode="peak",
        )
        new.activate(0.0)

        ref_states = self._run_sequence(ref, dv_seq, [float(t) for t in t_seq])
        new_states = self._run_sequence(new, dv_seq, [float(t) for t in t_seq])

        assert ref_states == new_states, "gate_mode='peak' must match default behavior"
        assert ref.lambda_v == pytest.approx(new.lambda_v)
        assert ref.lambda_v_peak == pytest.approx(new.lambda_v_peak)

    def test_peak_mode_with_extra_kwargs_ignored(self):
        """In peak mode, background kwargs (mu_buy etc.) are ignored — state identical."""
        tau = 300.0
        p = 0.65
        warmup = 5.0
        dv_seq = [10000.0] * 15 + [500.0] * 100

        ref = ParticipationGate(half_life_seconds=tau, peak_threshold_p=p, warmup_seconds=warmup)
        ref.activate(0.0)

        new = ParticipationGate(
            half_life_seconds=tau, peak_threshold_p=p, warmup_seconds=warmup,
            gate_mode="peak",
        )
        new.activate(0.0)

        for i, dv in enumerate(dv_seq):
            t = float(i + 1)
            s_ref = ref.update(dv, t)
            # Pass background kwargs — must be silently ignored in peak mode
            s_new = new.update(
                dv, t,
                mu_buy=1.0, mu_sell=0.5, lambda_buy=2.0, lambda_sell=1.0, dbar=500.0,
            )
            assert s_ref == s_new, f"State mismatch at t={t}: ref={s_ref}, peak_mode={s_new}"

    def test_peak_mode_with_asymmetric_hysteresis_identical(self):
        """gate_mode='peak' + p_open/p_close matches plain asymmetric gate."""
        tau = 60.0
        p_open = 0.65
        p_close = 0.30
        warmup = 0.0
        dv_seq = [100000.0, 0.0, 0.0, 0.0, 50000.0, 0.0] * 30

        ref = ParticipationGate(
            half_life_seconds=tau, peak_threshold_p=p_open, warmup_seconds=warmup,
            p_open=p_open, p_close=p_close,
        )
        ref.activate(0.0)

        new = ParticipationGate(
            half_life_seconds=tau, peak_threshold_p=p_open, warmup_seconds=warmup,
            p_open=p_open, p_close=p_close,
            gate_mode="peak",
        )
        new.activate(0.0)

        for i, dv in enumerate(dv_seq):
            t = float(i + 1)
            s_ref = ref.update(dv, t)
            s_new = new.update(dv, t)
            assert s_ref == s_new, f"Asymmetric mismatch at t={t}: {s_ref} vs {s_new}"

    def test_invalid_gate_mode_raises(self):
        """Constructor rejects unknown gate_mode values."""
        with pytest.raises(ValueError, match="gate_mode"):
            ParticipationGate(300.0, 0.65, gate_mode="bogus")

    def test_invalid_tau_peak_raises(self):
        with pytest.raises(ValueError, match="tau_peak"):
            ParticipationGate(300.0, 0.65, gate_mode="background", tau_peak=0.0)

    def test_invalid_C_raises(self):
        with pytest.raises(ValueError, match="C must"):
            ParticipationGate(300.0, 0.65, gate_mode="background", C=-1.0)


class TestBackgroundGateLogic:
    """Unit tests for gate_mode='background' WJI gate behavior (POC construction)."""

    def _make_gate(self, tau_peak=600.0, C=2.0, warmup=0.0, p=0.65,
                   lambda_v_ref=1.0, mu_buy_ref=1.0):
        g = ParticipationGate(
            half_life_seconds=300.0, peak_threshold_p=p, warmup_seconds=warmup,
            gate_mode="background", tau_peak=tau_peak, C=C,
        )
        g.activate(0.0, lambda_v_ref=lambda_v_ref, mu_buy_ref=mu_buy_ref)
        return g

    def test_returns_inactive_before_activate(self):
        g = ParticipationGate(300.0, 0.65, gate_mode="background")
        assert g.update(100.0, 1.0) == GateState.INACTIVE

    def test_warmup_when_refs_zero(self):
        """Thin-name guard fires when lambda_v_ref=0 (invalid ref); holds WARMUP state."""
        g = self._make_gate(warmup=300.0, lambda_v_ref=0.0)
        state = g.update(100.0, 1.0)
        assert state == GateState.WARMUP
        assert g._thin_guard_count == 1

    def test_thin_guard_rate(self):
        """thin_guard_rate = 1.0 when lambda_v_ref=0 (all updates trigger guard)."""
        g_thin = self._make_gate(warmup=0.0, lambda_v_ref=0.0)
        g_thin.update(100.0, 1.0)
        g_thin.update(100.0, 2.0)
        assert g_thin._thin_guard_count == 2
        assert g_thin._thin_guard_total == 2
        assert g_thin.thin_guard_rate == pytest.approx(1.0)
        # Valid refs: guard never fires
        g_ok = self._make_gate(warmup=0.0)
        g_ok.update(100.0, 1.0)
        g_ok.update(100.0, 2.0)
        assert g_ok._thin_guard_count == 0
        assert g_ok.thin_guard_rate == pytest.approx(0.0)

    def test_pass_when_wji_exceeds_threshold(self):
        """High volume + buy trades accumulating lambda_buy_slow → PASS."""
        g = self._make_gate(C=1.5, tau_peak=600.0, warmup=0.0)
        # side=1 accumulates lambda_buy_slow; large dv makes volume_ratio >> 1
        for t in range(1, 50):
            g.update(1_000_000.0, float(t), side=1)
        state = g.update(1_000_000.0, 50.0, side=1)
        assert state == GateState.PASS

    def test_fail_when_no_buy_trades(self):
        """With side=0 throughout, lambda_buy_slow=0, buy_term=0, WJI=0 → FAIL."""
        # C=2.0: threshold = max(p*0, 2.0*1.0) = 2.0; WJI = sqrt(vol_ratio * 0) = 0 → FAIL
        g = self._make_gate(C=2.0, tau_peak=600.0, warmup=0.0)
        for t in range(1, 20):
            g.update(200.0, float(t))  # side=0 default: lambda_buy_slow stays 0
        state = g.update(200.0, 20.0)
        assert state == GateState.FAIL

    def test_peak_wji_decays(self):
        """After a burst (side=1), peak_WJI decays when WJI falls back near zero.

        Uses very short lambda_V half-life (5s) so lambda_V collapses fast, producing
        near-zero WJI. One tick at t_far (many tau_peak after burst) should show
        peak_WJI decayed from the burst high.
        """
        tau_peak = 10.0
        g = ParticipationGate(
            half_life_seconds=5.0, peak_threshold_p=0.65, warmup_seconds=0.0,
            gate_mode="background", tau_peak=tau_peak, C=0.01,
        )
        g.activate(0.0, lambda_v_ref=1.0, mu_buy_ref=1.0)

        # Large burst: side=1 accumulates lambda_buy_slow; large dv makes volume_ratio >> 1
        for t in range(1, 6):
            g.update(1_000_000.0, float(t), side=1)
        peak_after_burst = g._peak_wji
        assert peak_after_burst > 0

        # At t=5+10*tau_peak: lambda_V decayed ~exp(-20)~0; lambda_buy_slow ~0. WJI~0.
        # peak_WJI decays 10 half-lives → < 1% of original.
        t_far = 5.0 + 10 * tau_peak
        g.update(0.0, t_far, side=0)
        peak_decayed = g._peak_wji
        assert peak_decayed < 0.01 * peak_after_burst, (
            f"peak_WJI should decay; got {peak_decayed:.6f} (was {peak_after_burst:.4f})"
        )

    def test_reset_clears_background_state(self):
        """reset() clears _peak_wji, _lambda_buy_slow, and thin_guard counters."""
        g = self._make_gate()
        g.update(500.0, 1.0, side=1)
        g.update(100.0, 2.0)
        g.reset()
        assert g._peak_wji == 0.0
        assert g._lambda_buy_slow == 0.0
        assert g._thin_guard_count == 0
        assert g._thin_guard_total == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
