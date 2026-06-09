"""
Unit tests for Phase EPG-GRT gate variant classes.

Covers AbsoluteThresholdGate (B), HawkesCumulativeGate (C),
HawkesBuySideGate (D), BurstRatioGate (E), WJISlowEMAGate (SEM).
Minimum 3 tests per class.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.epg.gate import GateState
from core.epg.gate_variants import (
    AbsoluteThresholdGate,
    BurstRatioGate,
    HawkesBuySideGate,
    HawkesCumulativeGate,
    WJISlowEMAGate,
)


# ══════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════


def _feed_pre_event(gate, n_seconds: int, dv_per_trade: float = 1000.0):
    """Feed one trade per second for n_seconds before activation."""
    for t in range(n_seconds):
        gate.update(dv_per_trade, float(t))


def _skip_warmup(gate, t_event: float, warmup: float = 300.0, dv: float = 0.0):
    """Advance gate past warmup by one tick."""
    gate.update(dv, t_event + warmup + 1.0)


# ══════════════════════════════════════════════════════════════════════
#  Variant B — AbsoluteThresholdGate
# ══════════════════════════════════════════════════════════════════════


class TestAbsoluteThresholdGate:

    def test_pass_fires_when_lambda_v_exceeds_k_times_ref(self):
        """PASS fires immediately once λ_V exceeds k_abs × λ_V_ref."""
        gate = AbsoluteThresholdGate(k_abs=2.0, half_life_seconds=300.0, warmup_seconds=10.0)

        # Pre-event: 120s of constant small volume to establish reference
        t_event = 120.0
        dv_small = 100.0
        for t in range(120):
            gate.update(dv_small, float(t))

        gate.activate(t_event)
        ref = gate.lambda_v_ref
        assert ref > 0, "Reference should be computed from pre-event data"
        assert not gate.fallback_used

        # Skip warmup with zero volume
        gate.update(0.0, t_event + 11.0)

        # Now feed high volume that exceeds 2 × ref
        # With τ=300s, λ_V_new = dv * decay_rate = dv * ln2/300
        # We need: dv * ln2/300 > 2 * ref
        # Set dv high enough
        decay_rate = math.log(2) / 300.0
        dv_high = (2.0 * ref / decay_rate) * 10.0  # 10× the threshold
        state = gate.update(dv_high, t_event + 12.0)
        assert state == GateState.PASS

    def test_does_not_fire_below_threshold(self):
        """Gate stays FAIL when λ_V < k_abs × λ_V_ref."""
        gate = AbsoluteThresholdGate(k_abs=5.0, half_life_seconds=300.0, warmup_seconds=10.0)

        # Pre-event: 120s establishing a reference
        for t in range(120):
            gate.update(2000.0, float(t))

        gate.activate(120.0)
        ref = gate.lambda_v_ref
        assert ref > 0

        # Skip warmup, then send zero volume — λ_V decays, cannot exceed 5 × ref
        gate.update(0.0, 121.0 + 10.0)  # past warmup
        state = gate.update(0.0, 200.0)
        assert state == GateState.FAIL

    def test_resets_correctly_on_reactivate(self):
        """reset() + activate() starts fresh with new reference."""
        gate = AbsoluteThresholdGate(k_abs=2.0, half_life_seconds=300.0, warmup_seconds=5.0)

        # Session 1
        for t in range(90):
            gate.update(500.0, float(t))
        gate.activate(90.0)
        ref1 = gate.lambda_v_ref

        # Reset for session 2
        gate.reset()
        assert gate.lambda_v == 0.0
        assert gate.lambda_v_ref == 0.0
        assert not gate._active

        # Session 2: different pre-event volume
        for t in range(60):
            gate.update(2000.0, float(t))
        gate.activate(60.0)
        ref2 = gate.lambda_v_ref

        # References should differ (different pre-event volumes)
        assert ref1 != ref2

    def test_inactive_before_activation(self):
        """Gate returns INACTIVE before T_event is set."""
        gate = AbsoluteThresholdGate(k_abs=3.0)
        state = gate.update(10000.0, 1.0)
        assert state == GateState.INACTIVE

    def test_warmup_period_observed(self):
        """Gate returns WARMUP for warmup_seconds after T_event."""
        gate = AbsoluteThresholdGate(k_abs=1.5, half_life_seconds=300.0, warmup_seconds=30.0)

        for t in range(90):
            gate.update(1000.0, float(t))
        gate.activate(90.0)

        state = gate.update(0.0, 90.0 + 15.0)  # within warmup
        assert state == GateState.WARMUP

    def test_fallback_used_when_pre_event_window_too_short(self):
        """Fallback triggers when T_event fires within first 60s of session."""
        fallback = 0.05
        gate = AbsoluteThresholdGate(k_abs=2.0, global_fallback_ref=fallback, warmup_seconds=5.0)

        # Only 30s of pre-event data — insufficient
        for t in range(30):
            gate.update(500.0, float(t))
        gate.activate(30.0)

        assert gate.fallback_used
        assert gate.lambda_v_ref == fallback


# ══════════════════════════════════════════════════════════════════════
#  Variant C — HawkesCumulativeGate
# ══════════════════════════════════════════════════════════════════════


class TestHawkesCumulativeGate:

    def _make_gate(self, beta=0.01, k=2.0, mu=0.2, warmup=10.0):
        return HawkesCumulativeGate(beta_slow=beta, k_slow=k, mu_cum=mu, warmup_seconds=warmup)

    def test_signal_decays_at_beta_slow_after_no_trades(self):
        """λ_cum decay factor between consecutive trades is exp(-β_slow × dt).

        Each update() call represents a trade arrival and always adds β_slow.
        The decay between arrivals is verified: λ_cum_2 = λ_cum_1 × exp(-β × dt) + β.
        """
        beta = 0.01
        gate = self._make_gate(beta=beta, warmup=0.0)
        gate.activate(t_event=0.0)

        # Trade 1 at t=1s (dt=1 from activate): λ_cum = 0 × exp(-β×1) + β = β
        gate.update(0.0, 1.0, side=1)
        lam1 = gate.lambda_cum
        assert abs(lam1 - beta) < 1e-12, f"First trade: expected β={beta}, got {lam1}"

        # Trade 2 at t=101s (dt=100s later): λ_cum = lam1 × exp(-β×100) + β
        gate.update(0.0, 101.0, side=0)
        lam2 = gate.lambda_cum
        expected = lam1 * math.exp(-beta * 100.0) + beta
        assert abs(lam2 - expected) < 1e-12, (
            f"Second trade: expected {expected:.8f}, got {lam2:.8f}"
        )

    def test_fires_when_cumulative_intensity_exceeds_k_times_mu(self):
        """PASS fires when λ_cum > k_slow × μ_cum after warmup."""
        beta = 0.01
        k = 2.0
        mu = 0.2
        threshold = k * mu  # = 0.4

        gate = self._make_gate(beta=beta, k=k, mu=mu, warmup=0.0)
        gate.activate(t_event=0.0)

        # Feed many trades rapidly so λ_cum builds up above threshold.
        # At steady state (uniform dt→0), λ_cum = β_slow * n_trades_per_interval.
        # With dt=0.1s, we need λ_cum > 0.4:
        # λ_cum converges to β_slow / (1 - exp(-β_slow × dt)) ≈ β_slow / (β_slow × dt) = 1/dt
        # For dt=0.1: steady state ≈ 10 >> 0.4 ✓
        state = GateState.FAIL
        for i in range(1, 200):
            state = gate.update(100.0, float(i) * 0.1)
            if state == GateState.PASS:
                break
        assert state == GateState.PASS

    def test_side_is_irrelevant(self):
        """Buy and sell trades increment λ_cum identically."""
        beta = 0.01
        gate_buy = self._make_gate(beta=beta, warmup=0.0)
        gate_sell = self._make_gate(beta=beta, warmup=0.0)

        gate_buy.activate(0.0)
        gate_sell.activate(0.0)

        for t in range(1, 50):
            gate_buy.update(500.0, float(t), side=1)   # buys
            gate_sell.update(500.0, float(t), side=-1)  # sells

        assert abs(gate_buy.lambda_cum - gate_sell.lambda_cum) < 1e-12

    def test_inactive_before_activation(self):
        """Gate returns INACTIVE before activate()."""
        gate = self._make_gate()
        state = gate.update(1000.0, 1.0, side=1)
        assert state == GateState.INACTIVE

    def test_reset_clears_state(self):
        """reset() clears λ_cum and deactivates gate."""
        gate = self._make_gate(warmup=0.0)
        gate.activate(0.0)
        for t in range(1, 20):
            gate.update(1000.0, float(t))
        assert gate.lambda_cum > 0

        gate.reset()
        assert gate.lambda_cum == 0.0
        assert not gate._active

        state = gate.update(1000.0, 50.0)
        assert state == GateState.INACTIVE

    def test_set_mu_updates_reference(self):
        """set_mu() changes the PASS threshold correctly."""
        gate = self._make_gate(k=2.0, mu=0.1, warmup=0.0)  # threshold = 0.2
        gate.activate(0.0)

        # Build to a moderate level, verify PASS
        for t in range(1, 100):
            gate.update(500.0, float(t) * 0.1)

        state_before = gate.update(0.0, 100.0)

        # Raise threshold by setting mu to a much larger value
        gate.set_mu(10.0)  # threshold = 20.0 — well above any reachable λ_cum
        state_after = gate.update(0.0, 101.0)
        assert state_after == GateState.FAIL


# ══════════════════════════════════════════════════════════════════════
#  Variant D — HawkesBuySideGate
# ══════════════════════════════════════════════════════════════════════


class TestHawkesBuySideGate:

    def _make_gate(self, beta=0.01, k=2.0, mu_buy=0.1, warmup=10.0):
        return HawkesBuySideGate(
            beta_slow=beta, k_slow=k, mu_buy=mu_buy, warmup_seconds=warmup
        )

    def test_sell_events_do_not_increment_lambda_buy(self):
        """Sell trades decay λ_buy but do not add to it."""
        gate = self._make_gate(beta=0.01, warmup=0.0)
        gate.activate(0.0)

        # Single buy to seed non-zero value
        gate.update(500.0, 1.0, side=1)
        lam_after_buy = gate.lambda_buy
        assert lam_after_buy > 0

        # Now a sell at t=2 — should only decay, not add
        gate.update(500.0, 2.0, side=-1)
        expected = lam_after_buy * math.exp(-0.01 * 1.0)
        assert abs(gate.lambda_buy - expected) < 1e-12

    def test_buy_events_increment_lambda_buy(self):
        """Buy trades increment λ_buy by β_slow."""
        beta = 0.01
        gate = self._make_gate(beta=beta, warmup=0.0)
        gate.activate(0.0)

        # t=0 is activate time; first update with a sell, no decay (dt=0)
        gate.update(0.0, 0.0, side=-1)  # no increment
        assert gate.lambda_buy == 0.0

        # Buy trade at t=0.001 (near-zero dt, no meaningful decay)
        gate.update(500.0, 0.001, side=1)
        # Expected: 0 * exp(-beta*0.001) + beta ≈ beta
        assert abs(gate.lambda_buy - beta) < 1e-5

    def test_pass_fires_when_lambda_buy_exceeds_threshold(self):
        """PASS fires when λ_buy > k_slow × μ_buy after warmup."""
        beta = 0.01
        k = 2.0
        mu_buy = 0.1
        gate = self._make_gate(beta=beta, k=k, mu_buy=mu_buy, warmup=0.0)
        gate.activate(0.0)

        # Rapid buy-only trades — λ_buy builds toward β_slow / (β_slow × dt) = 1/dt
        # With dt=0.1: steady state ≈ 10 >> threshold=0.2
        state = GateState.FAIL
        for i in range(1, 200):
            state = gate.update(100.0, float(i) * 0.1, side=1)
            if state == GateState.PASS:
                break
        assert state == GateState.PASS

    def test_pure_sells_cannot_open_gate(self):
        """Gate stays FAIL if only sell trades arrive."""
        gate = self._make_gate(beta=0.01, k=1.5, mu_buy=0.1, warmup=0.0)
        gate.activate(0.0)

        for i in range(1, 500):
            state = gate.update(10000.0, float(i) * 0.01, side=-1)

        assert state == GateState.FAIL
        assert gate.lambda_buy == pytest.approx(0.0, abs=1e-6)

    def test_reset_clears_state(self):
        """reset() clears λ_buy and deactivates gate."""
        gate = self._make_gate(warmup=0.0)
        gate.activate(0.0)
        for i in range(1, 50):
            gate.update(500.0, float(i), side=1)
        assert gate.lambda_buy > 0

        gate.reset()
        assert gate.lambda_buy == 0.0
        assert not gate._active

        state = gate.update(500.0, 100.0, side=1)
        assert state == GateState.INACTIVE


# ══════════════════════════════════════════════════════════════════════
#  Variant E — BurstRatioGate
# ══════════════════════════════════════════════════════════════════════


class TestBurstRatioGate:

    def _make_gate(self, window=60.0, threshold_r=2.0, warmup=10.0):
        return BurstRatioGate(window_n=window, threshold_r=threshold_r, warmup_seconds=warmup)

    def test_burst_ratio_exceeds_one_during_burst(self):
        """r(t) > 1.0 when fast EMA is larger than slow EMA during a burst."""
        gate = self._make_gate(window=30.0, threshold_r=5.0, warmup=0.0)
        gate.activate(0.0)

        # Steady low volume first (so slow EMA is low)
        for i in range(1, 60):
            gate.update(100.0, float(i))

        # Now a burst of high volume
        gate.update(100000.0, 60.0)
        gate.update(100000.0, 61.0)
        gate.update(100000.0, 62.0)

        r = gate.burst_ratio
        assert r > 1.0, f"Expected r > 1.0 during burst, got {r:.4f}"

    def test_burst_ratio_approaches_one_at_steady_state(self):
        """r(t) converges toward 1.0 when volume is constant for long enough."""
        gate = self._make_gate(window=30.0, threshold_r=2.0, warmup=0.0)
        gate.activate(0.0)

        # Feed constant volume for many half-lives of the slow EMA (τ_slow=120s)
        # After ~10 half-lives the EMAs should both converge to the same value
        for i in range(1, 1500):
            gate.update(500.0, float(i))

        r = gate.burst_ratio
        # Allow tolerance; fast and slow converge but never exactly equal
        assert abs(r - 1.0) < 0.02, f"Expected r ≈ 1.0 at steady state, got {r:.6f}"

    def test_pass_fail_transitions_on_threshold_crossing(self):
        """Gate transitions PASS↔FAIL correctly on threshold crossing."""
        gate = self._make_gate(window=60.0, threshold_r=2.0, warmup=0.0)
        gate.activate(0.0)

        # Build steady state (r ≈ 1.0, below threshold=2.0)
        for i in range(1, 1500):
            gate.update(500.0, float(i))
        assert gate.update(500.0, 1500.0) == GateState.FAIL

        # Burst: r spikes above threshold_r=2.0
        state = GateState.FAIL
        for j in range(1501, 1540):
            state = gate.update(500000.0, float(j))
            if state == GateState.PASS:
                break
        assert state == GateState.PASS, "Expected PASS during burst"

        # After burst subsides (zero volume for long decay), r returns below threshold
        state_post = gate.update(0.0, 2500.0)
        assert state_post == GateState.FAIL

    def test_inactive_before_activation(self):
        """Gate returns INACTIVE before activate()."""
        gate = self._make_gate()
        assert gate.update(10000.0, 1.0) == GateState.INACTIVE

    def test_reset_clears_state(self):
        """reset() clears both EMAs and deactivates gate."""
        gate = self._make_gate(warmup=0.0)
        gate.activate(0.0)
        for i in range(1, 100):
            gate.update(1000.0, float(i))
        assert gate.lambda_v_fast > 0
        assert gate.lambda_v_slow > 0

        gate.reset()
        assert gate.lambda_v_fast == 0.0
        assert gate.lambda_v_slow == 0.0
        assert not gate._active
        assert gate.update(1000.0, 200.0) == GateState.INACTIVE

    def test_warmup_period_respected(self):
        """Gate returns WARMUP for warmup_seconds after T_event."""
        gate = self._make_gate(window=60.0, threshold_r=1.1, warmup=30.0)
        gate.activate(0.0)

        state = gate.update(1000000.0, 15.0)  # within warmup, even with big burst
        assert state == GateState.WARMUP


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ══════════════════════════════════════════════════════════════════════
#  WJISlowEMAGate (SEM) tests — T2c
# ══════════════════════════════════════════════════════════════════════

class TestWJISlowEMAGate:
    """Unit tests for WJISlowEMAGate (Phase WJI-SlowEMA, T2c)."""

    def _make_gate(
        self,
        tau_slow: float = 300.0,
        p_open: float = 0.80,
        p_close: float = 0.55,
        warmup: float = 0.0,
    ) -> WJISlowEMAGate:
        return WJISlowEMAGate(
            tau_slow=tau_slow, p_open=p_open, p_close=p_close, warmup_seconds=warmup
        )

    def _drive_to_pass(
        self, gate: WJISlowEMAGate, wji_val: float = 1.0, n: int = 1
    ) -> GateState:
        """Send n ticks of wji_val × 1.5 (well above p_open=0.80) with dt=1."""
        state = GateState.INACTIVE
        for _ in range(n):
            state = gate.update(wji_val * 1.5, 1.0)
        return state

    # -- T2c test 1: fast tau tracks signal quickly
    def test_fast_tau_ema_tracks_signal(self):
        """With small tau_slow the EMA converges to the input in a few half-lives."""
        tau = 10.0
        gate = self._make_gate(tau_slow=tau, warmup=0.0)
        gate.activate(0.0)

        # Seed with initial value 1.0
        gate.update(1.0, 0.0)
        assert math.isclose(gate.wji_slow, 1.0)

        # Drive with 5.0 for 3 × tau (= 30 s active); expect EMA > 4.0
        t = 0.0
        for _ in range(30):
            gate.update(5.0, 1.0)
            t += 1.0
        # After 3 × half-life the EMA should be > 87.5% of the way to 5.0
        assert gate.wji_slow > 4.0, f"Expected fast convergence, got wji_slow={gate.wji_slow:.4f}"

    # -- T2c test 2: slow tau keeps EMA stable against a transient spike
    def test_slow_tau_resists_transient_spike(self):
        """With large tau_slow a single spike barely moves the EMA."""
        tau = 1800.0
        gate = self._make_gate(tau_slow=tau, warmup=0.0)
        gate.activate(0.0)

        # Establish baseline EMA at 1.0 with many 1-second ticks
        for _ in range(300):
            gate.update(1.0, 1.0)
        baseline = gate.wji_slow

        # One spike tick with dt=1 should barely move the EMA
        gate.update(10.0, 1.0)
        delta = gate.wji_slow - baseline
        assert delta < 0.01, f"Slow EMA moved too much on spike: delta={delta:.6f}"

    # -- T2c test 3: halt gap (large dt) decays EMA toward zero, then re-initialises
    def test_halt_gap_large_dt_decays_ema(self):
        """After a halt (large dt), EMA decays significantly toward zero."""
        tau = 300.0
        gate = self._make_gate(tau_slow=tau, warmup=0.0)
        gate.activate(0.0)

        # Establish EMA near 2.0
        for _ in range(200):
            gate.update(2.0, 1.0)
        pre_halt = gate.wji_slow

        # Simulate a 30-minute halt (1800 active seconds with wji=0 — effectively zero contrib)
        # Use a zero-WJI tick with the halt dt so EMA decays to near zero
        gate.update(0.0, 1800.0)
        post_halt = gate.wji_slow

        # After 6 half-lives the EMA should be < 2% of pre-halt value
        decay_expected = math.exp(-math.log(2) * 1800.0 / tau)
        assert post_halt < pre_halt * decay_expected * 1.05, (
            f"EMA did not decay: pre={pre_halt:.4f} post={post_halt:.4f} decay={decay_expected:.4f}"
        )

    # -- T2c test 4: dead zone holds current state (no transition in dead zone)
    def test_dead_zone_holds_state(self):
        """WJI in dead zone (p_close <= WJI < p_open * wji_slow) should not change state."""
        gate = self._make_gate(tau_slow=300.0, p_open=0.80, p_close=0.55, warmup=0.0)
        gate.activate(0.0)

        # Seed EMA at 1.0
        gate.update(1.0, 0.0)
        # Force into PASS by sending WJI above p_open threshold
        gate.update(gate.wji_slow * 0.80 * 1.1, 1.0)
        assert gate.update(gate.wji_slow * 0.80 * 1.1, 1.0) == GateState.PASS

        # Now send a dead-zone tick: p_close < wji < p_open (e.g., 0.70 × wji_slow)
        wji_dead = gate.wji_slow * 0.70
        state = gate.update(wji_dead, 1.0)
        assert state == GateState.PASS, (
            f"Dead zone tick changed state to {state}; expected PASS"
        )

    # -- T2c test 5: FAIL→PASS only at p_open, not at p_close
    def test_fail_to_pass_only_at_p_open(self):
        """Gate opens at p_open threshold; p_close alone never triggers PASS."""
        gate = self._make_gate(tau_slow=300.0, p_open=0.80, p_close=0.55, warmup=0.0)
        gate.activate(0.0)

        # Seed EMA
        gate.update(1.0, 0.0)
        # Confirm gate starts FAIL
        state = gate.update(0.5, 1.0)
        assert state == GateState.FAIL

        # Send a tick at exactly p_close * wji_slow — should NOT open
        tick_at_pclose = gate.wji_slow * gate._p_close
        state = gate.update(tick_at_pclose, 1.0)
        assert state == GateState.FAIL, (
            f"Gate opened at p_close level; expected FAIL, got {state}"
        )

        # Send a tick at p_open * wji_slow — now should open
        tick_at_popen = gate.wji_slow * gate._p_open
        state = gate.update(tick_at_popen, 1.0)
        assert state == GateState.PASS, (
            f"Gate did not open at p_open level; expected PASS, got {state}"
        )
