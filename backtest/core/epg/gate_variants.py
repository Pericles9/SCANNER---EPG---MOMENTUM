"""
Gate variants for Phase EPG-GRT and EPG-OPT2.

All implement the same interface: .activate(t_event), .update(dollar_vol, timestamp, side),
.reset().  The side argument is +1=buy, -1=sell, 0=unknown.

Variants:
  AbsoluteThresholdGate   (B) — λ_V vs fixed pre-event mean reference; no peak ratchet
  HawkesCumulativeGate    (C) — slow arrival-rate kernel (buy+sell) vs μ_background
  HawkesBuySideGate       (D) — slow arrival-rate kernel (buy-only) vs μ_buy
  BurstRatioGate          (E) — fast/slow EMA ratio; fires at volume inflection point
  SlopeGate               (F) — opens on λ_V acceleration; two sub-variants:
                                 F_ss (slope open / slope close)
                                 F_sl (slope open / level close)
"""
from __future__ import annotations

import logging
import math
from collections import deque
from typing import Deque, List, Optional, Tuple

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
