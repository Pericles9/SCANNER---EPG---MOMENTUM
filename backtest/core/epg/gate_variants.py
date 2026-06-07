"""
Gate variants for Phase EPG-GRT, EPG-OPT2, WJI-POC, and WJI-OPT.

All implement the same interface: .activate(t_event), .update(...), .reset().

Variants:
  AbsoluteThresholdGate   (B) — λ_V vs fixed pre-event mean reference; no peak ratchet
  HawkesCumulativeGate    (C) — slow arrival-rate kernel (buy+sell) vs μ_background
  HawkesBuySideGate       (D) — slow arrival-rate kernel (buy-only) vs μ_buy
  BurstRatioGate          (E) — fast/slow EMA ratio; fires at volume inflection point
  SlopeGate               (F) — opens on λ_V acceleration; two sub-variants:
                                 F_ss (slope open / slope close)
                                 F_sl (slope open / level close)
  WJIGate                 (WJI) — joint buy-side arrival × dollar-volume signal;
                                   geometric mean of normalised components; slope-driven
                                   adaptive peak with continuous decay on deceleration
  RunningMaxGate          (RMG) — signal-agnostic running-max threshold gate;
                                   accepts pre-normalised signal; no decay; hysteresis
                                   toggle: 'single' (symmetric) or 'asym' (asymmetric)
"""
from __future__ import annotations

import logging
import math
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

from core.epg.gate import GateState

log = logging.getLogger(__name__)

LN2 = math.log(2)
WARMUP_DURATION_SEC = 300.0
_PRE_EVENT_MIN_WINDOW_SEC = 60.0   # minimum pre-event window for Variant B reference


# ══════════════════════════════════════════════════════════════════════
#  Variant B — AbsoluteThresholdGate
# ══════════════════════════════════════════════════════════════════════

class AbsoluteThresholdGate:
    """
    Variant B: dollar volume EMA compared against a fixed pre-event mean reference.

    Reference computation:
        λ_V_ref = mean(λ_V) over [T_session_start, T_event)

    If T_event fires within the first 60s of the session (insufficient pre-event window),
    falls back to global_fallback_ref and logs the occurrence.

    PASS condition (no hysteresis):
        λ_V(t) > k_abs × λ_V_ref
    """

    def __init__(
        self,
        k_abs: float,
        half_life_seconds: float = 300.0,
        global_fallback_ref: float = 0.0,
        warmup_seconds: float = WARMUP_DURATION_SEC,
    ):
        if k_abs <= 0:
            raise ValueError(f"k_abs must be positive, got {k_abs}")
        if half_life_seconds <= 0:
            raise ValueError(f"half_life_seconds must be positive, got {half_life_seconds}")

        self.k_abs = k_abs
        self.half_life = half_life_seconds
        self._decay_rate = LN2 / half_life_seconds
        self.global_fallback_ref = global_fallback_ref
        self.warmup_seconds = warmup_seconds

        self._lambda_v: float = 0.0
        self._last_timestamp: Optional[float] = None
        self._pre_event_start_t: Optional[float] = None
        self._pre_event_lambdas: List[float] = []
        self._t_event: Optional[float] = None
        self._active: bool = False
        self._lambda_v_ref: float = 0.0
        self.fallback_used: bool = False

    def activate(self, t_event: float) -> None:
        self._t_event = t_event
        self._active = True

        pre_window = (
            (t_event - self._pre_event_start_t)
            if self._pre_event_start_t is not None else 0.0
        )
        if (pre_window >= _PRE_EVENT_MIN_WINDOW_SEC
                and self._pre_event_lambdas):
            self._lambda_v_ref = sum(self._pre_event_lambdas) / len(self._pre_event_lambdas)
            self.fallback_used = False
        else:
            self._lambda_v_ref = self.global_fallback_ref
            self.fallback_used = True
            log.warning(
                "AbsoluteThresholdGate: pre-event window %.1fs < 60s or no data; "
                "using global fallback λ_V_ref=%.6f",
                pre_window, self._lambda_v_ref,
            )

    def update(self, dollar_vol: float, timestamp: float, side: int = 0) -> GateState:
        # Update EMA (runs from first trade, including pre-event period)
        if self._last_timestamp is None:
            self._pre_event_start_t = timestamp
            self._lambda_v = dollar_vol * self._decay_rate
        else:
            dt = max(0.0, timestamp - self._last_timestamp)
            self._lambda_v = (
                self._lambda_v * math.exp(-self._decay_rate * dt)
                + dollar_vol * self._decay_rate
            )
        self._last_timestamp = timestamp

        if not self._active:
            self._pre_event_lambdas.append(self._lambda_v)
            return GateState.INACTIVE

        time_since = timestamp - self._t_event
        if time_since < self.warmup_seconds:
            return GateState.WARMUP

        ref = self._lambda_v_ref
        if ref > 0 and self._lambda_v > self.k_abs * ref:
            return GateState.PASS
        return GateState.FAIL

    def reset(self) -> None:
        self._lambda_v = 0.0
        self._last_timestamp = None
        self._pre_event_start_t = None
        self._pre_event_lambdas = []
        self._t_event = None
        self._active = False
        self._lambda_v_ref = 0.0
        self.fallback_used = False

    @property
    def lambda_v(self) -> float:
        return self._lambda_v

    @property
    def lambda_v_ref(self) -> float:
        return self._lambda_v_ref


# ══════════════════════════════════════════════════════════════════════
#  Variant C — HawkesCumulativeGate
# ══════════════════════════════════════════════════════════════════════

class HawkesCumulativeGate:
    """
    Variant C: slow Hawkes-style arrival-rate kernel over all trades (buy+sell).

    Signal:
        λ_cum(t_i) = λ_cum(t_{i-1}) × exp(-β_slow × dt) + β_slow

    Each trade (buy or sell) contributes β_slow events/second.  Units: events/s.
    Reference: μ_cum = mu_buy + mu_sell from cold-start MLE fit.

    PASS condition:
        λ_cum(t) > k_slow × μ_cum
    """

    def __init__(
        self,
        beta_slow: float,
        k_slow: float,
        mu_cum: float = 0.2,
        warmup_seconds: float = WARMUP_DURATION_SEC,
    ):
        if beta_slow <= 0:
            raise ValueError(f"beta_slow must be positive, got {beta_slow}")
        if k_slow <= 0:
            raise ValueError(f"k_slow must be positive, got {k_slow}")
        if mu_cum <= 0:
            raise ValueError(f"mu_cum must be positive, got {mu_cum}")

        self.beta_slow = beta_slow
        self.k_slow = k_slow
        self.mu_cum = mu_cum
        self.warmup_seconds = warmup_seconds

        self._lambda_cum: float = 0.0
        self._t_event: Optional[float] = None
        self._last_timestamp: Optional[float] = None
        self._active: bool = False

    def set_mu(self, mu_cum: float) -> None:
        """Update μ_cum from cold-start fit. Call before first post-activation update."""
        if mu_cum <= 0:
            raise ValueError(f"mu_cum must be positive, got {mu_cum}")
        self.mu_cum = mu_cum

    def activate(self, t_event: float) -> None:
        self._t_event = t_event
        self._last_timestamp = t_event
        self._lambda_cum = 0.0
        self._active = True

    def update(self, dollar_vol: float, timestamp: float, side: int = 0) -> GateState:
        if not self._active:
            return GateState.INACTIVE

        dt = max(0.0, timestamp - self._last_timestamp)
        # Decay + one trade contribution (buy or sell, size-independent)
        self._lambda_cum = self._lambda_cum * math.exp(-self.beta_slow * dt) + self.beta_slow
        self._last_timestamp = timestamp

        time_since = timestamp - self._t_event
        if time_since < self.warmup_seconds:
            return GateState.WARMUP

        if self._lambda_cum > self.k_slow * self.mu_cum:
            return GateState.PASS
        return GateState.FAIL

    def reset(self) -> None:
        self._lambda_cum = 0.0
        self._t_event = None
        self._last_timestamp = None
        self._active = False

    @property
    def lambda_cum(self) -> float:
        return self._lambda_cum


# ══════════════════════════════════════════════════════════════════════
#  Variant D — HawkesBuySideGate
# ══════════════════════════════════════════════════════════════════════

class HawkesBuySideGate:
    """
    Variant D: slow Hawkes-style arrival-rate kernel over buy-side trades only.

    Signal:
        λ_buy(t_i) = λ_buy(t_{i-1}) × exp(-β_slow × dt) + β_slow   [if side == +1]
        λ_buy(t_i) = λ_buy(t_{i-1}) × exp(-β_slow × dt)             [if side != +1]

    Reference: μ_buy from cold-start MLE fit.

    PASS condition:
        λ_buy(t) > k_slow × μ_buy
    """

    def __init__(
        self,
        beta_slow: float,
        k_slow: float,
        mu_buy: float = 0.1,
        warmup_seconds: float = WARMUP_DURATION_SEC,
    ):
        if beta_slow <= 0:
            raise ValueError(f"beta_slow must be positive, got {beta_slow}")
        if k_slow <= 0:
            raise ValueError(f"k_slow must be positive, got {k_slow}")
        if mu_buy <= 0:
            raise ValueError(f"mu_buy must be positive, got {mu_buy}")

        self.beta_slow = beta_slow
        self.k_slow = k_slow
        self.mu_buy = mu_buy
        self.warmup_seconds = warmup_seconds

        self._lambda_buy: float = 0.0
        self._t_event: Optional[float] = None
        self._last_timestamp: Optional[float] = None
        self._active: bool = False

    def set_mu_buy(self, mu_buy: float) -> None:
        """Update μ_buy from cold-start fit. Call before first post-activation update."""
        if mu_buy <= 0:
            raise ValueError(f"mu_buy must be positive, got {mu_buy}")
        self.mu_buy = mu_buy

    def activate(self, t_event: float) -> None:
        self._t_event = t_event
        self._last_timestamp = t_event
        self._lambda_buy = 0.0
        self._active = True

    def update(self, dollar_vol: float, timestamp: float, side: int = 0) -> GateState:
        if not self._active:
            return GateState.INACTIVE

        dt = max(0.0, timestamp - self._last_timestamp)
        decay = math.exp(-self.beta_slow * dt)
        self._lambda_buy *= decay
        if side == 1:
            self._lambda_buy += self.beta_slow
        self._last_timestamp = timestamp

        time_since = timestamp - self._t_event
        if time_since < self.warmup_seconds:
            return GateState.WARMUP

        if self._lambda_buy > self.k_slow * self.mu_buy:
            return GateState.PASS
        return GateState.FAIL

    def reset(self) -> None:
        self._lambda_buy = 0.0
        self._t_event = None
        self._last_timestamp = None
        self._active = False

    @property
    def lambda_buy(self) -> float:
        return self._lambda_buy


# ══════════════════════════════════════════════════════════════════════
#  Variant E — BurstRatioGate
# ══════════════════════════════════════════════════════════════════════

class BurstRatioGate:
    """
    Variant E: ratio of fast to slow dollar-volume EMA.  Fires at the inflection
    point of a volume burst rather than after the burst is established.

    Signal:
        τ_fast = window_n,  τ_slow = window_n × 4
        λ_V_fast(t_i) = λ_V_fast(t_{i-1}) × exp(-ln2 × dt / τ_fast) + dv × (ln2 / τ_fast)
        λ_V_slow(t_i) = λ_V_slow(t_{i-1}) × exp(-ln2 × dt / τ_slow) + dv × (ln2 / τ_slow)
        r(t) = λ_V_fast / max(λ_V_slow, ε)   where ε = 1e-6

    PASS condition:
        r(t) > threshold_r
    """

    _EPS = 1e-6

    def __init__(
        self,
        window_n: float,
        threshold_r: float,
        warmup_seconds: float = WARMUP_DURATION_SEC,
    ):
        if window_n <= 0:
            raise ValueError(f"window_n must be positive, got {window_n}")
        if threshold_r <= 0:
            raise ValueError(f"threshold_r must be positive, got {threshold_r}")

        self.window_n = window_n
        self.threshold_r = threshold_r
        self.warmup_seconds = warmup_seconds

        tau_fast = window_n
        tau_slow = window_n * 4.0
        self._decay_fast = LN2 / tau_fast
        self._decay_slow = LN2 / tau_slow

        self._lambda_v_fast: float = 0.0
        self._lambda_v_slow: float = 0.0
        self._t_event: Optional[float] = None
        self._last_timestamp: Optional[float] = None
        self._active: bool = False

    def activate(self, t_event: float) -> None:
        self._t_event = t_event
        self._last_timestamp = t_event
        self._lambda_v_fast = 0.0
        self._lambda_v_slow = 0.0
        self._active = True

    def update(self, dollar_vol: float, timestamp: float, side: int = 0) -> GateState:
        if not self._active:
            return GateState.INACTIVE

        dt = max(0.0, timestamp - self._last_timestamp)
        dv = dollar_vol
        self._lambda_v_fast = (
            self._lambda_v_fast * math.exp(-self._decay_fast * dt)
            + dv * self._decay_fast
        )
        self._lambda_v_slow = (
            self._lambda_v_slow * math.exp(-self._decay_slow * dt)
            + dv * self._decay_slow
        )
        self._last_timestamp = timestamp

        time_since = timestamp - self._t_event
        if time_since < self.warmup_seconds:
            return GateState.WARMUP

        r = self._lambda_v_fast / max(self._lambda_v_slow, self._EPS)
        return GateState.PASS if r > self.threshold_r else GateState.FAIL

    def reset(self) -> None:
        self._lambda_v_fast = 0.0
        self._lambda_v_slow = 0.0
        self._t_event = None
        self._last_timestamp = None
        self._active = False

    @property
    def lambda_v_fast(self) -> float:
        return self._lambda_v_fast

    @property
    def lambda_v_slow(self) -> float:
        return self._lambda_v_slow

    @property
    def burst_ratio(self) -> float:
        return self._lambda_v_fast / max(self._lambda_v_slow, self._EPS)


# ══════════════════════════════════════════════════════════════════════
#  Variant F — SlopeGate
# ══════════════════════════════════════════════════════════════════════

class SlopeGate:
    """
    Variant F: opens when λ_V is *accelerating* rather than when it crosses a level.

    Core signal:
        slope_V(t) = (λ_V(t) − λ_V(t − L_sec)) / L_sec
        norm_slope(t) = slope_V(t) / λ_V_ref

    λ_V_ref is the cold-start background dollar volume rate (mu_buy + mu_sell in
    dollar-volume units). L_sec is the lookback distance in seconds.

    Two sub-variants controlled by mode parameter:

    F_ss (mode='ss') — slope open, slope close:
        FAIL → PASS: norm_slope ≥ k_open
        PASS → FAIL: norm_slope < k_close
        Dead band: k_close ≤ norm_slope < k_open → hold current state

    F_sl (mode='sl') — slope open, level close:
        FAIL → PASS: norm_slope ≥ k_open
        PASS → FAIL: λ_V(t) < p_close × running_peak
        Running peak resets on each FAIL→PASS transition.
        Dead band applies only to open transition.

    In both modes, norm_slope is undefined (FAIL returned) until L_sec seconds of
    history are available after activation. The lookback buffer holds (timestamp, λ_V)
    pairs; stale entries are pruned on each update.
    """

    def __init__(
        self,
        tau_sec: float,
        L_sec: float,
        k_open: float,
        mode: str = "ss",
        k_close: float = -1.0,
        p_close: float = 0.35,
        lambda_v_ref: float = 1.0,
        warmup_seconds: float = WARMUP_DURATION_SEC,
    ):
        """
        Parameters
        ----------
        tau_sec : float
            Half-life (seconds) for the underlying λ_V EMA.
        L_sec : float
            Lookback distance (seconds) for slope computation.
        k_open : float
            Normalised slope threshold for FAIL→PASS transition.
        mode : str
            'ss' = slope open / slope close; 'sl' = slope open / level close.
        k_close : float
            Normalised slope threshold for PASS→FAIL in mode='ss'. Must be < k_open.
            Ignored when mode='sl'.
        p_close : float
            Level-close fraction of running peak in mode='sl'. Must be in (0, 1].
            Ignored when mode='ss'.
        lambda_v_ref : float
            Background dollar volume rate used to normalise slope. Must be > 0.
        warmup_seconds : float
            Duration of WARMUP period after T_event.
        """
        if tau_sec <= 0:
            raise ValueError(f"tau_sec must be positive, got {tau_sec}")
        if L_sec <= 0:
            raise ValueError(f"L_sec must be positive, got {L_sec}")
        if mode not in ("ss", "sl"):
            raise ValueError(f"mode must be 'ss' or 'sl', got {mode!r}")
        if mode == "ss" and k_close >= k_open:
            raise ValueError(
                f"k_close ({k_close}) must be < k_open ({k_open}) in mode='ss'"
            )
        if mode == "sl" and not (0 < p_close <= 1.0):
            raise ValueError(f"p_close must be in (0, 1], got {p_close}")
        if lambda_v_ref <= 0:
            raise ValueError(f"lambda_v_ref must be positive, got {lambda_v_ref}")

        self.tau_sec = tau_sec
        self.L_sec = L_sec
        self.k_open = k_open
        self.mode = mode
        self.k_close = k_close
        self.p_close = p_close
        self.lambda_v_ref = lambda_v_ref
        self.warmup_seconds = warmup_seconds

        self._decay_rate = LN2 / tau_sec

        # Gate state
        self._lambda_v: float = 0.0
        self._t_event: Optional[float] = None
        self._last_timestamp: Optional[float] = None
        self._active: bool = False
        self._in_pass: bool = False

        # Lookback buffer: deque of (timestamp, lambda_v)
        self._buf: Deque[Tuple[float, float]] = deque()

        # Level-close (F_sl) running peak
        self._lambda_v_peak: float = 0.0

    def activate(self, t_event: float) -> None:
        self._t_event = t_event
        self._last_timestamp = t_event
        self._lambda_v = 0.0
        self._lambda_v_peak = 0.0
        self._active = True
        self._in_pass = False
        self._buf.clear()

    def update(self, dollar_vol: float, timestamp: float, side: int = 0) -> GateState:
        if not self._active or self._t_event is None:
            return GateState.INACTIVE

        dt = max(0.0, timestamp - self._last_timestamp)
        decay = math.exp(-self._decay_rate * dt)
        self._lambda_v = self._lambda_v * decay + dollar_vol * self._decay_rate
        self._last_timestamp = timestamp

        # Append current (timestamp, lambda_v) to lookback buffer
        self._buf.append((timestamp, self._lambda_v))

        # Prune: remove entries from the front as long as the SECOND entry also
        # predates the cutoff. This always keeps buf[0] as the most recent entry
        # with ts ≤ cutoff — necessary for computing lv_past with irregular ticks.
        cutoff = timestamp - self.L_sec
        while len(self._buf) > 1 and self._buf[1][0] <= cutoff:
            self._buf.popleft()

        # Check warmup
        if timestamp - self._t_event < self.warmup_seconds:
            return GateState.WARMUP

        # buf[0] is the most recent entry at or before cutoff (after pruning).
        # If it is still newer than cutoff, we don't have enough history.
        if len(self._buf) == 0 or self._buf[0][0] > cutoff:
            return GateState.FAIL
        lv_past = self._buf[0][1]

        norm_slope = (self._lambda_v - lv_past) / (self.L_sec * max(self.lambda_v_ref, 1e-9))

        if self.mode == "ss":
            return self._update_ss(norm_slope)
        else:
            return self._update_sl(norm_slope)

    def _update_ss(self, norm_slope: float) -> GateState:
        """slope open / slope close with dead band."""
        if self._in_pass:
            if norm_slope < self.k_close:
                self._in_pass = False
            # else: hold (dead band or above k_close)
        else:
            if norm_slope >= self.k_open:
                self._in_pass = True
            # else: hold (below k_open)
        return GateState.PASS if self._in_pass else GateState.FAIL

    def _update_sl(self, norm_slope: float) -> GateState:
        """slope open / level close."""
        if self._in_pass:
            # Update running peak
            if self._lambda_v > self._lambda_v_peak:
                self._lambda_v_peak = self._lambda_v
            # Level close: λ_V < p_close × running_peak
            if self._lambda_v_peak > 0 and self._lambda_v < self.p_close * self._lambda_v_peak:
                self._in_pass = False
                self._lambda_v_peak = 0.0
        else:
            # Open on slope ≥ k_open
            if norm_slope >= self.k_open:
                self._in_pass = True
                self._lambda_v_peak = self._lambda_v
        return GateState.PASS if self._in_pass else GateState.FAIL

    def reset(self) -> None:
        self._lambda_v = 0.0
        self._lambda_v_peak = 0.0
        self._t_event = None
        self._last_timestamp = None
        self._active = False
        self._in_pass = False
        self._buf.clear()

    @property
    def lambda_v(self) -> float:
        return self._lambda_v

    @property
    def lambda_v_peak(self) -> float:
        """Running peak (used only in F_sl mode)."""
        return self._lambda_v_peak

    @property
    def norm_slope(self) -> float:
        """Most recently computed normalised slope (0.0 if buffer insufficient)."""
        if not self._buf or self._last_timestamp is None:
            return 0.0
        cutoff = self._last_timestamp - self.L_sec
        if self._buf[0][0] > cutoff:
            return 0.0
        lv_past = self._buf[0][1]
        return (self._lambda_v - lv_past) / (self.L_sec * max(self.lambda_v_ref, 1e-9))


# ══════════════════════════════════════════════════════════════════════
#  WJI — WJIGate
# ══════════════════════════════════════════════════════════════════════

class WJIGate:
    """
    Weighted Joint Intensity Gate (Phase WJI-POC).

    Combines a dollar-volume EMA (λ_V) and a buy-side arrival kernel (λ_buy_slow)
    via a geometric mean, producing the joint signal WJI(t).  The running peak
    of WJI is adaptive: it accumulates when WJI is accelerating and decays
    continuously when WJI is decelerating and the gate is in FAIL.

    Signal:
        norm_λ_V(t)   = λ_V(t) / λ_V_ref
        norm_λ_buy(t) = λ_buy_slow(t) / μ_buy
        WJI(t)        = norm_λ_V(t)^α × norm_λ_buy(t)^(1−α)

    λ_buy_slow kernel:
        λ_buy_slow += β_slow  on buy trade;  always decays at rate β_slow

    Peak update rules:
        During PASS or (FAIL and slope ≥ 0):  peak = max(peak, WJI)
        During FAIL and slope < 0:            peak decays with half-life τ_decay

    PASS condition (asymmetric hysteresis):
        FAIL → PASS: WJI(t) ≥ p_open  × peak
        PASS → FAIL: WJI(t) <  p_close × peak

    Activate signature differs from other variants: activate(t_event, λ_V_ref, μ_buy).
    Peak is initialised at 1.0 so the gate will not open until WJI meaningfully
    exceeds background level on both components.
    """

    _EPS: float = 1e-9

    def __init__(
        self,
        alpha: float = 0.5,
        tau_v: float = 180.0,
        beta_slow: float = 0.01,
        L_sec: float = 60.0,
        tau_decay: float = 120.0,
        p_open: float = 0.65,
        p_close: float = 0.30,
        warmup_seconds: float = WARMUP_DURATION_SEC,
    ):
        """
        Parameters
        ----------
        alpha : float
            Volume component weight in [0, 1].  alpha=0.5 → equal blend.
        tau_v : float
            Half-life (seconds) for the λ_V dollar-volume EMA.
        beta_slow : float
            Decay rate for the buy-side arrival kernel.  ~ln2/β = half-life.
        L_sec : float
            Lookback distance (seconds) for WJI slope computation.
        tau_decay : float
            Half-life (seconds) for peak decay during FAIL+deceleration periods.
        p_open : float
            FAIL→PASS opening threshold as a fraction of peak.  Must be in (0, 1].
        p_close : float
            PASS→FAIL closing threshold as a fraction of peak.  Must satisfy
            0 < p_close ≤ p_open.
        warmup_seconds : float
            Duration of WARMUP period after T_event.
        """
        if not (0.0 <= alpha <= 1.0):
            raise ValueError(f"alpha must be in [0, 1], got {alpha}")
        if tau_v <= 0:
            raise ValueError(f"tau_v must be positive, got {tau_v}")
        if beta_slow <= 0:
            raise ValueError(f"beta_slow must be positive, got {beta_slow}")
        if L_sec <= 0:
            raise ValueError(f"L_sec must be positive, got {L_sec}")
        if tau_decay <= 0:
            raise ValueError(f"tau_decay must be positive, got {tau_decay}")
        if not (0 < p_close <= p_open <= 1.0):
            raise ValueError(
                f"need 0 < p_close ≤ p_open ≤ 1, got p_close={p_close}, p_open={p_open}"
            )

        self.alpha = alpha
        self.tau_v = tau_v
        self.beta_slow = beta_slow
        self.L_sec = L_sec
        self.tau_decay = tau_decay
        self.p_open = p_open
        self.p_close = p_close
        self.warmup_seconds = warmup_seconds

        self._decay_rate_v: float = LN2 / tau_v
        self._decay_rate_peak: float = LN2 / tau_decay

        # Signal state
        self._lambda_v: float = 0.0
        self._lambda_buy_slow: float = 0.0
        self._wji: float = 0.0
        self._peak: float = 1.0
        self._in_pass: bool = False

        # Activation state
        self._t_event: Optional[float] = None
        self._last_timestamp: Optional[float] = None
        self._active: bool = False
        self._lambda_v_ref: float = 1.0
        self._mu_buy: float = 1.0

        # WJI slope lookback buffer: deque of (timestamp, WJI)
        self._wji_buffer: Deque[Tuple[float, float]] = deque()

        # Per-PASS-window records: list of dicts with keys
        #   window_open_time, peak_at_open, peak_at_prior_close
        self._pass_windows: List[Dict[str, float]] = []
        self._last_pass_close_peak: float = 0.0  # peak at most-recent PASS→FAIL
        self._first_window: bool = True           # True until first PASS window closes

    def activate(self, t_event: float, lambda_v_ref: float, mu_buy: float) -> None:
        """
        Initialise the gate for a new event.

        Parameters
        ----------
        t_event : float
            Timestamp when T_event was established.
        lambda_v_ref : float
            Background dollar-volume rate (mean λ_V over [session_start, T_event)).
            Used to normalise λ_V.  Must be > 0.
        mu_buy : float
            Cold-start buy arrival rate from Hawkes MLE fit (mu_buy).
            Used to normalise λ_buy_slow.  Must be > 0.
        """
        if lambda_v_ref <= 0:
            raise ValueError(f"lambda_v_ref must be positive, got {lambda_v_ref}")
        if mu_buy <= 0:
            raise ValueError(f"mu_buy must be positive, got {mu_buy}")

        self._t_event = t_event
        self._last_timestamp = t_event
        self._lambda_v = 0.0
        self._lambda_buy_slow = 0.0
        self._wji = 0.0
        self._peak = 1.0
        self._in_pass = False
        self._active = True
        self._lambda_v_ref = lambda_v_ref
        self._mu_buy = mu_buy
        self._wji_buffer.clear()
        self._pass_windows = []
        self._last_pass_close_peak = 0.0
        self._first_window = True

    def update(self, dollar_vol: float, timestamp: float, side: int = 0) -> GateState:
        """
        Process one trade and return gate state.

        Parameters
        ----------
        dollar_vol : float
            Dollar volume of this trade (price × size).
        timestamp : float
            Trade timestamp (must be ≥ previous timestamp).
        side : int
            +1 = buy, −1 = sell, 0 = unknown.  Only buys increment λ_buy_slow.
        """
        if not self._active or self._t_event is None:
            return GateState.INACTIVE

        dt = max(0.0, timestamp - self._last_timestamp)

        # ── λ_V EMA ──────────────────────────────────────────────────
        self._lambda_v = (
            self._lambda_v * math.exp(-self._decay_rate_v * dt)
            + dollar_vol * self._decay_rate_v
        )

        # ── λ_buy_slow kernel ────────────────────────────────────────
        self._lambda_buy_slow *= math.exp(-self.beta_slow * dt)
        if side == 1:
            self._lambda_buy_slow += self.beta_slow

        self._last_timestamp = timestamp

        # ── WJI (geometric mean of normalised components) ────────────
        norm_v = self._lambda_v / max(self._lambda_v_ref, self._EPS)
        norm_buy = self._lambda_buy_slow / max(self._mu_buy, self._EPS)
        self._wji = (
            max(norm_v, self._EPS) ** self.alpha
            * max(norm_buy, self._EPS) ** (1.0 - self.alpha)
        )

        # ── WJI slope buffer ─────────────────────────────────────────
        self._wji_buffer.append((timestamp, self._wji))
        cutoff = timestamp - self.L_sec
        while len(self._wji_buffer) > 1 and self._wji_buffer[1][0] <= cutoff:
            self._wji_buffer.popleft()

        # ── Warmup gate ───────────────────────────────────────────────
        if timestamp - self._t_event < self.warmup_seconds:
            return GateState.WARMUP

        # ── Slope computation ────────────────────────────────────────
        # slope_defined when buffer[0] predates the cutoff
        slope_defined = (
            len(self._wji_buffer) >= 1
            and self._wji_buffer[0][0] <= cutoff
        )

        if not slope_defined:
            # Pre-history period: accumulate peak but gate cannot open (FAIL only)
            if self._wji > self._peak:
                self._peak = self._wji
            return GateState.FAIL

        slope_wji = self._wji - self._wji_buffer[0][1]  # sign only used

        # ── Peak update ───────────────────────────────────────────────
        # Decay only occurs during FAIL when slope is negative.
        if self._in_pass or slope_wji >= 0.0:
            if self._wji > self._peak:
                self._peak = self._wji
        else:
            # FAIL + slope < 0: continuous decay
            if dt > 0:
                self._peak *= math.exp(-self._decay_rate_peak * dt)

        # ── Asymmetric hysteresis ─────────────────────────────────────
        if self._in_pass:
            if self._wji < self.p_close * self._peak:
                # PASS → FAIL
                self._last_pass_close_peak = self._peak
                self._first_window = False
                self._in_pass = False
        else:
            if self._wji >= self.p_open * self._peak:
                # FAIL → PASS: record window entry
                prior_close = 0.0 if self._first_window else self._last_pass_close_peak
                self._pass_windows.append({
                    "window_open_time": timestamp,
                    "peak_at_open": self._peak,
                    "peak_at_prior_close": prior_close,
                })
                self._in_pass = True

        return GateState.PASS if self._in_pass else GateState.FAIL

    def reset(self) -> None:
        """Reset all state for a new session or event."""
        self._lambda_v = 0.0
        self._lambda_buy_slow = 0.0
        self._wji = 0.0
        self._peak = 1.0
        self._in_pass = False
        self._t_event = None
        self._last_timestamp = None
        self._active = False
        self._lambda_v_ref = 1.0
        self._mu_buy = 1.0
        self._wji_buffer.clear()
        self._pass_windows = []
        self._last_pass_close_peak = 0.0
        self._first_window = True

    # ── Properties ────────────────────────────────────────────────────

    @property
    def wji(self) -> float:
        """Most recently computed WJI value."""
        return self._wji

    @property
    def peak(self) -> float:
        """Current running peak of WJI."""
        return self._peak

    @property
    def norm_lambda_v(self) -> float:
        """Normalised dollar-volume component: λ_V / λ_V_ref."""
        return self._lambda_v / max(self._lambda_v_ref, self._EPS)

    @property
    def norm_lambda_buy(self) -> float:
        """Normalised buy-arrival component: λ_buy_slow / μ_buy."""
        return self._lambda_buy_slow / max(self._mu_buy, self._EPS)

    @property
    def pass_windows(self) -> List[Dict[str, float]]:
        """
        Per-PASS-window records.  Each dict has keys:
            window_open_time     — timestamp of FAIL→PASS transition
            peak_at_open         — peak value at that moment
            peak_at_prior_close  — peak at most-recent prior PASS→FAIL (0.0 if first)
        """
        return self._pass_windows

    @property
    def slope_wji(self) -> float:
        """Most recently computed WJI slope: WJI(t) − WJI(t−L_sec). 0.0 if undefined."""
        if not self._wji_buffer or self._last_timestamp is None:
            return 0.0
        cutoff = self._last_timestamp - self.L_sec
        if self._wji_buffer[0][0] > cutoff:
            return 0.0
        return self._wji - self._wji_buffer[0][1]


# ══════════════════════════════════════════════════════════════════════
#  RunningMaxGate — signal-agnostic running-max threshold (WJI-OPT)
# ══════════════════════════════════════════════════════════════════════

class RunningMaxGate:
    """
    Running-max threshold gate (Phase WJI-OPT).

    Signal-agnostic: caller computes the normalised signal (WJI or norm_λ_V) and
    passes a single float to update().  The gate applies a causal running-max
    peak with no decay and no slope term.

    Peak initialises at 1.0 (background level for normalised signals) and is
    monotonically non-decreasing — it never falls.

    Hysteresis modes
    ----------------
    'single':  FAIL→PASS at signal ≥ p × peak
               PASS→FAIL at signal < p × peak       (symmetric)
    'asym':    FAIL→PASS at signal ≥ p × peak
               PASS→FAIL at signal < p_close × peak  (p_close=0.30 default)

    The peak is updated before the threshold check on every tick (including
    during warmup), so the threshold ratchets up as the signal makes new highs.
    """

    def __init__(
        self,
        p: float,
        hysteresis: str = "asym",
        p_close: float = 0.30,
        warmup_seconds: float = WARMUP_DURATION_SEC,
    ):
        """
        Parameters
        ----------
        p : float
            FAIL→PASS opening threshold as fraction of peak.  In 'single' mode,
            also used as the PASS→FAIL close threshold.  Must be in (0, 1].
        hysteresis : str
            'single' or 'asym'.
        p_close : float
            PASS→FAIL close threshold in 'asym' mode.
            Must satisfy 0 < p_close ≤ p.  Ignored in 'single' mode.
        warmup_seconds : float
            Duration of WARMUP period after t_event.
        """
        if hysteresis not in ("single", "asym"):
            raise ValueError(f"hysteresis must be 'single' or 'asym', got {hysteresis!r}")
        if not (0.0 < p <= 1.0):
            raise ValueError(f"p must be in (0, 1], got {p}")
        if hysteresis == "asym" and not (0.0 < p_close <= p):
            raise ValueError(
                f"p_close must satisfy 0 < p_close <= p={p}, got p_close={p_close}"
            )

        self.p = p
        self.hysteresis = hysteresis
        self.p_close = p_close if hysteresis == "asym" else p
        self.warmup_seconds = warmup_seconds

        self._peak: float = 1.0
        self._in_pass: bool = False
        self._t_event: Optional[float] = None
        self._active: bool = False

    def activate(self, t_event: float) -> None:
        """Initialise the gate for a new event."""
        self._t_event = t_event
        self._peak = 1.0
        self._in_pass = False
        self._active = True

    def update(self, signal: float, timestamp: float) -> GateState:
        """
        Process one pre-normalised signal value and return gate state.

        Parameters
        ----------
        signal : float
            Normalised intensity (e.g., WJI or norm_λ_V).  Values are expected
            near 1.0 at background; gate opens when signal is meaningfully above
            background relative to the running peak.
        timestamp : float
            Absolute timestamp in seconds.
        """
        if not self._active or self._t_event is None:
            return GateState.INACTIVE

        # Peak is always monotonically non-decreasing (updated on every tick)
        if signal > self._peak:
            self._peak = signal

        if timestamp - self._t_event < self.warmup_seconds:
            return GateState.WARMUP

        if self._in_pass:
            if signal < self.p_close * self._peak:
                self._in_pass = False
        else:
            if signal >= self.p * self._peak:
                self._in_pass = True

        return GateState.PASS if self._in_pass else GateState.FAIL

    def reset(self) -> None:
        """Reset all state."""
        self._peak = 1.0
        self._in_pass = False
        self._t_event = None
        self._active = False

    @property
    def peak(self) -> float:
        """Current running-max peak value."""
        return self._peak
