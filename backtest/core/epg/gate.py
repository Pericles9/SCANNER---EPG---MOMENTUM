"""
ParticipationGate — Dollar volume intensity gate for the EPG.

Tracks exponentially decaying dollar volume intensity lambda_V(t) and
compares it to a running peak threshold. The gate returns one of three
states: WARMUP, PASS, or FAIL.

Formula:
    lambda_V(t_i) = lambda_V(t_{i-1}) * exp(-ln2 * dt / tau) + dv_i * (ln2 / tau)

where dv_i = price_i * size_i (dollar volume of the trade).

The running peak is causal: lambda_V_peak at time t = max(lambda_V[T_event:t]).
The threshold ratchets up with each new peak — it never decreases.

Peak cooling: when the gate has been in FAIL for >= m_cool_sec seconds, the running
peak decays with half-life tau_cool_sec. This prevents the ratchet from locking out
re-entry after a multi-leg move. Set m_cool_sec=0 to disable (default, no change to
existing behavior).
"""
from __future__ import annotations

import enum
import logging
import math
from typing import Optional

import numpy as np


log = logging.getLogger(__name__)
LN2 = math.log(2)
WARMUP_DURATION_SEC = 300.0  # 5 minutes


class GateState(enum.Enum):
    """State of the participation gate."""
    WARMUP = "WARMUP"   # First 5 minutes after T_event — gate inactive
    PASS = "PASS"       # lambda_V >= p * running_peak
    FAIL = "FAIL"       # lambda_V < p * running_peak
    INACTIVE = "INACTIVE"  # T_event not yet established


class ParticipationGate:
    """Dollar volume intensity gate with running peak threshold and optional hysteresis."""

    def __init__(
        self,
        half_life_seconds: float,
        peak_threshold_p: float,
        warmup_seconds: float = WARMUP_DURATION_SEC,
        *,
        p_open: Optional[float] = None,
        p_close: Optional[float] = None,
        m_cool_sec: float = 0.0,
        tau_cool_sec: float = 120.0,
        gate_mode: str = "peak",
        tau_peak: float = 600.0,
        C: float = 2.0,
        beta_slow: float = 0.01,
        k: float = 1.0,
        h: float = 4.0,
        sigma_log_fallback: float = 0.209,
        lambda_h: float = 0.01,
        p_enter: float = 0.80,
        prior_mean_std: float = 1.0,
        dir_thresh_mult: float = 1.0,
        max_run_length: int = 600,
    ):
        """
        Parameters
        ----------
        half_life_seconds : float
            Exponential decay half-life for lambda_V (e.g. 300 for 5 min).
        peak_threshold_p : float
            Fraction of running peak for symmetric PASS (e.g. 0.65). When
            p_open/p_close are omitted, both default to this value, reproducing
            the original symmetric behavior exactly.
        warmup_seconds : float
            Duration of WARMUP period after T_event (default 300s = 5 min).
        p_open : float, optional
            FAIL→PASS opening threshold fraction. Defaults to peak_threshold_p.
        p_close : float, optional
            PASS→FAIL closing threshold fraction. Defaults to p_open.
            Must satisfy p_close <= p_open.
        m_cool_sec : float
            Minimum continuous FAIL duration (seconds) before peak cooling
            activates. Set to 0 to disable cooling entirely (default).
        tau_cool_sec : float
            Half-life (seconds) of peak decay once cooling is active.
            Ignored when m_cool_sec=0.
        """
        if half_life_seconds <= 0:
            raise ValueError(f"half_life must be positive, got {half_life_seconds}")
        if not (0 < peak_threshold_p <= 1.0):
            raise ValueError(f"peak_threshold_p must be in (0, 1], got {peak_threshold_p}")
        if m_cool_sec < 0:
            raise ValueError(f"m_cool_sec must be >= 0, got {m_cool_sec}")
        if m_cool_sec > 0 and tau_cool_sec <= 0:
            raise ValueError(f"tau_cool_sec must be positive when cooling enabled, got {tau_cool_sec}")

        _p_open = p_open if p_open is not None else peak_threshold_p
        _p_close = p_close if p_close is not None else _p_open
        if not (0 < _p_open <= 1.0):
            raise ValueError(f"p_open must be in (0, 1], got {_p_open}")
        if not (0 < _p_close <= 1.0):
            raise ValueError(f"p_close must be in (0, 1], got {_p_close}")
        if _p_close > _p_open:
            raise ValueError(f"p_close ({_p_close}) must be <= p_open ({_p_open})")
        if gate_mode not in ("peak", "background", "cusum", "bocpd"):
            raise ValueError(
                f"gate_mode must be 'peak', 'background', 'cusum' or 'bocpd', got {gate_mode!r}"
            )
        if tau_peak <= 0:
            raise ValueError(f"tau_peak must be positive, got {tau_peak}")
        if C <= 0:
            raise ValueError(f"C must be positive, got {C}")
        if beta_slow <= 0:
            raise ValueError(f"beta_slow must be positive, got {beta_slow}")
        if gate_mode == "cusum":
            if k <= 0:
                raise ValueError(f"cusum k must be positive, got {k}")
            if h <= 0:
                raise ValueError(f"cusum h must be positive, got {h}")
            if sigma_log_fallback <= 0:
                raise ValueError(f"cusum sigma_log_fallback must be positive, got {sigma_log_fallback}")
        if gate_mode == "bocpd":
            if not (0.0 < lambda_h < 1.0):
                raise ValueError(f"bocpd lambda_h must be in (0, 1), got {lambda_h}")
            # p_exit = p_enter - 0.10 (fixed hysteresis gap); require p_exit > 0.
            if not (0.10 < p_enter <= 1.0):
                raise ValueError(f"bocpd p_enter must be in (0.10, 1.0], got {p_enter}")
            if prior_mean_std <= 0:
                raise ValueError(f"bocpd prior_mean_std must be positive, got {prior_mean_std}")
            if dir_thresh_mult < 0:
                raise ValueError(f"bocpd dir_thresh_mult must be >= 0, got {dir_thresh_mult}")
            if max_run_length < 1:
                raise ValueError(f"bocpd max_run_length must be >= 1, got {max_run_length}")
            if sigma_log_fallback <= 0:
                raise ValueError(f"bocpd sigma_log_fallback must be positive, got {sigma_log_fallback}")

        self.half_life = half_life_seconds
        self.peak_threshold_p = peak_threshold_p
        self.p_open = _p_open
        self.p_close = _p_close
        self.warmup_seconds = warmup_seconds
        self.m_cool_sec = m_cool_sec
        self.tau_cool_sec = tau_cool_sec
        self.gate_mode = gate_mode
        self.tau_peak = tau_peak
        self.C = C
        self.beta_slow = beta_slow
        self.k = k
        self.h = h
        self.sigma_log_fallback = sigma_log_fallback
        # BOCPD params (gate_mode="bocpd"). lambda_h / p_enter are the 2 swept knobs;
        # the rest are fixed, documented model constants (NOT tuned). p_exit is derived
        # from p_enter with a hard-coded 0.10 hysteresis gap.
        self.lambda_h = lambda_h
        self.p_enter = p_enter
        self.p_exit = p_enter - 0.10
        self.prior_mean_std = prior_mean_std      # sigma0: prior std on a run's mean (WJI_log units)
        self.dir_thresh_mult = dir_thresh_mult    # kappa_dir: surge threshold = kappa_dir * sigma_log
        self.max_run_length = int(max_run_length)

        # Precompute decay constant: ln2 / tau
        self._decay_rate = LN2 / half_life_seconds

        # State
        self._lambda_v: float = 0.0
        self._lambda_v_peak: float = 0.0
        self._t_event: Optional[float] = None
        self._last_timestamp: Optional[float] = None
        self._active = False
        self._in_pass: bool = False

        # Peak cooling state (peak mode only)
        self._fail_start_ts: Optional[float] = None
        self._cooling_active: bool = False
        self._peak_at_cool_start: float = 0.0
        self._cool_start_ts: Optional[float] = None

        # Background mode state (POC construction: lambda_buy_slow / mu_buy_ref)
        self._peak_wji: float = 0.0
        self._lambda_buy_slow: float = 0.0
        self._lambda_v_ref: float = 1.0
        self._mu_buy_ref: float = 1.0
        self._thin_guard_count: int = 0
        self._thin_guard_total: int = 0
        self._last_bg_debug: dict = {}

        # CUSUM mode state (gate_mode="cusum"). The one-sided upper CUSUM accumulates
        # standardised log-ratio deviations of WJI from its background. It is a purely
        # per-tick recursion (no dt term), so it naturally "holds" across halts: halted
        # ticks are skipped and S_up is left untouched.
        self._s_up: float = 0.0                       # accumulated regime-entry evidence
        self._sigma_log: Optional[float] = None       # finalised at warmup exit
        self._warmup_logs: list = []                  # log-ratios collected during warmup
        self._sigma_finalized: bool = False
        self._last_cusum_debug: dict = {}

        # BOCPD mode state (gate_mode="bocpd"). Run-length posterior + per-run-length
        # sufficient statistics for a Normal-Normal (known variance sigma_log^2, unknown
        # run mean, prior mean 0) conjugate model. Reuses the cusum warmup-sigma machinery
        # (_warmup_logs / _sigma_log / _sigma_finalized). R/count/sum are allocated at
        # warmup exit. See _update_bocpd for the full algorithm + directional P_regime.
        self._bo_R: Optional[np.ndarray] = None       # run-length posterior (normalized)
        self._bo_count: Optional[np.ndarray] = None    # obs count per run-length hypothesis
        self._bo_sum: Optional[np.ndarray] = None      # sum of WJI_log per run-length hypothesis
        self._bo_len: int = 0                          # number of valid hypotheses (0..len-1)
        self._bo_initialized: bool = False
        self._p_regime: float = 0.0
        self._last_bocpd_debug: dict = {}

    @property
    def last_bg_debug(self) -> dict:
        """Internal WJI state from the most recent background-mode update()."""
        return self._last_bg_debug

    @property
    def s_up(self) -> float:
        """Current CUSUM upper accumulator (cusum mode). Primary decision variable."""
        return self._s_up

    @property
    def sigma_log(self) -> Optional[float]:
        """Finalised per-event sigma_log (cusum mode), or None before warmup exit."""
        return self._sigma_log

    @property
    def last_cusum_debug(self) -> dict:
        """Internal CUSUM state from the most recent cusum-mode update() (for charts)."""
        return self._last_cusum_debug

    @property
    def p_regime(self) -> float:
        """Directional surge-regime probability from the latest bocpd update (0 before activation)."""
        return self._p_regime

    @property
    def bocpd_R(self) -> Optional[np.ndarray]:
        """Current BOCPD run-length posterior (copy), or None before warmup exit (for charts)."""
        if self._bo_R is None or self._bo_len == 0:
            return None
        return self._bo_R[: self._bo_len].copy()

    @property
    def last_bocpd_debug(self) -> dict:
        """Internal BOCPD state from the most recent bocpd-mode update() (for charts)."""
        return self._last_bocpd_debug

    @property
    def lambda_v(self) -> float:
        """Current dollar volume intensity."""
        return self._lambda_v

    @property
    def lambda_v_peak(self) -> float:
        """Running peak of lambda_V since T_event."""
        return self._lambda_v_peak

    @property
    def t_event(self) -> Optional[float]:
        """T_event timestamp, or None if not yet set."""
        return self._t_event

    @property
    def threshold(self) -> float:
        """Opening threshold = p_open * running_peak (FAIL→PASS boundary)."""
        return self.p_open * self._lambda_v_peak

    @property
    def threshold_close(self) -> float:
        """Closing threshold = p_close * running_peak (PASS→FAIL boundary)."""
        return self.p_close * self._lambda_v_peak

    @property
    def thin_guard_rate(self) -> float:
        """Fraction of background-mode ticks that triggered the thin-name guard."""
        if self._thin_guard_total == 0:
            return 0.0
        return self._thin_guard_count / self._thin_guard_total

    def activate(
        self,
        t_event: float,
        *,
        lambda_v_ref: float = 1.0,
        mu_buy_ref: float = 1.0,
    ) -> None:
        """
        Called when T_event is first established by EventAnchor.

        Resets all internal state and begins tracking lambda_V from this
        moment. The first `warmup_seconds` after t_event return WARMUP.

        Parameters
        ----------
        t_event : float
            Timestamp when T_event was detected (same time domain as
            update() timestamps).
        lambda_v_ref : float, keyword-only
            Pre-event mean of lambda_V EMA over [session_start, T_event).
            Used in background mode to normalise volume: volume_ratio = lambda_V / lambda_v_ref.
            Static — frozen at activation. Ignored in peak mode.
        mu_buy_ref : float, keyword-only
            Cold-start Hawkes mu_buy at T_event (from initial fit, not online refit).
            Used in background mode to normalise the buy-side slow EMA: buy_term = lambda_buy_slow / mu_buy_ref.
            Static — frozen at activation. Ignored in peak mode.
        """
        self._t_event = t_event
        self._lambda_v = 0.0
        self._lambda_v_peak = 0.0
        self._last_timestamp = t_event
        self._active = True
        self._fail_start_ts = None
        self._cooling_active = False
        self._peak_at_cool_start = 0.0
        self._cool_start_ts = None
        self._peak_wji = 0.0
        self._lambda_buy_slow = 0.0
        self._lambda_v_ref = lambda_v_ref
        self._mu_buy_ref = mu_buy_ref
        self._thin_guard_count = 0
        self._thin_guard_total = 0
        self._last_bg_debug = {}
        # CUSUM state resets only here (event start) — never between PASS/FAIL cycles.
        self._s_up = 0.0
        self._sigma_log = None
        self._warmup_logs = []
        self._sigma_finalized = False
        self._last_cusum_debug = {}
        # BOCPD state resets only here (event start) — never between PASS/FAIL cycles.
        self._bo_R = None
        self._bo_count = None
        self._bo_sum = None
        self._bo_len = 0
        self._bo_initialized = False
        self._p_regime = 0.0
        self._last_bocpd_debug = {}

    def update(
        self,
        dollar_vol: float = 0.0,
        timestamp: float = 0.0,
        side: int = 0,
        *,
        mu_buy: float = 0.0,
        mu_sell: float = 0.0,
        lambda_buy: float = 0.0,
        lambda_sell: float = 0.0,
        dbar: float = 0.0,
        wji: Optional[float] = None,
        wji_background: float = 1.0,
        is_halted: bool = False,
    ) -> GateState:
        """
        Update lambda_V with a new trade and return gate state.

        Parameters
        ----------
        dollar_vol : float
            Dollar volume of this trade (price * size).
        timestamp : float
            Trade timestamp (seconds from session start, or absolute —
            must be consistent with t_event).
        side : int, optional
            Trade side (+1 buy, -1 sell, 0 unknown). Ignored in peak mode;
            included for interface uniformity with gate_variants classes.
        mu_buy, mu_sell : float, keyword-only
            Hawkes background rates from latest online refit (background mode only).
        lambda_buy, lambda_sell : float, keyword-only
            Live Hawkes intensities at this tick (background mode only).
        dbar : float, keyword-only
            Mean dollar-per-trade over the refit window (background mode only).

        Returns
        -------
        GateState
            INACTIVE if T_event not set, WARMUP if within warmup period,
            PASS/FAIL based on threshold (peak mode: p*peak_λV;
            background mode: WJI vs max(p*peak_WJI, C*WJI_background)).
        """
        if not self._active or self._t_event is None:
            return GateState.INACTIVE

        # ── CUSUM mode (gate_mode="cusum") ──
        # Branches early: does not touch the λ_V / peak / background machinery below.
        if self.gate_mode == "cusum":
            return self._update_cusum(wji, wji_background, timestamp, is_halted)

        # ── BOCPD mode (gate_mode="bocpd") ──
        # Same early branch: consumes the pre-computed (locked) WJI / WJI_background.
        if self.gate_mode == "bocpd":
            return self._update_bocpd(wji, wji_background, timestamp, is_halted)

        # Compute dt and decay
        dt = timestamp - self._last_timestamp
        if dt < 0:
            dt = 0.0  # protect against out-of-order timestamps

        # Exponential decay + new contribution
        decay = math.exp(-self._decay_rate * dt)
        self._lambda_v = self._lambda_v * decay + dollar_vol * self._decay_rate
        self._last_timestamp = timestamp

        # Check warmup
        time_since_t_event = timestamp - self._t_event

        if self.gate_mode == "background":
            # ── Background (WJI) mode — POC construction ──
            # Buy-side slow EMA (independent of Hawkes; accumulates on buy trades).
            # Half-life ≈ ln2/beta_slow ≈ 69s with default beta_slow=0.01.
            self._lambda_buy_slow *= math.exp(-self.beta_slow * dt)
            if side == 1:
                self._lambda_buy_slow += self.beta_slow

            self._thin_guard_total += 1
            _EPS = 1e-9
            if self._lambda_v_ref < _EPS or self._mu_buy_ref < _EPS:
                # Guard: invalid reference values (thin name or bad activation).
                self._thin_guard_count += 1
                if time_since_t_event < self.warmup_seconds:
                    return GateState.WARMUP
                return GateState.PASS if self._in_pass else GateState.FAIL

            # WJI = sqrt(volume_ratio × buy_term)
            #   volume_ratio = lambda_V / lambda_v_ref  (static pre-event mean)
            #   buy_term     = lambda_buy_slow / mu_buy_ref  (POC; background → 1.0)
            #   wji_bg       = sqrt(g(1) × 1.0) = 1.0  (constant with POC)
            volume_ratio = self._lambda_v / self._lambda_v_ref
            buy_term = self._lambda_buy_slow / self._mu_buy_ref
            wji_sq = volume_ratio * buy_term
            wji = math.sqrt(wji_sq) if wji_sq > 0.0 else 0.0
            wji_bg = 1.0

            peak_decay_bg = math.exp(-dt / self.tau_peak) if dt > 0.0 else 1.0
            self._peak_wji = max(wji, self._peak_wji * peak_decay_bg)

            if time_since_t_event < self.warmup_seconds:
                return GateState.WARMUP

            threshold_open = max(self.p_open * self._peak_wji, self.C * wji_bg)
            threshold_close = max(self.p_close * self._peak_wji, self.C * wji_bg)
            if self._in_pass:
                if wji < threshold_close:
                    self._in_pass = False
            else:
                if wji >= threshold_open:
                    self._in_pass = True
            self._last_bg_debug = {
                "wji": wji,
                "wji_bg": wji_bg,
                "peak_wji": self._peak_wji,
                "p_peak": self.p_open * self._peak_wji,
                "volume_ratio": volume_ratio,
                "lambda_buy_slow": self._lambda_buy_slow,
                "lambda_v": self._lambda_v,
                "lambda_v_ref": self._lambda_v_ref,
                "mu_buy_ref": self._mu_buy_ref,
                "threshold_open": threshold_open,
                "threshold_close": threshold_close,
                "in_pass": self._in_pass,
            }
            return GateState.PASS if self._in_pass else GateState.FAIL

        # ── Peak mode (existing behavior, bit-for-bit) ──
        if time_since_t_event < self.warmup_seconds:
            # Still accumulate peak during warmup (normal path)
            if self._lambda_v > self._lambda_v_peak:
                self._lambda_v_peak = self._lambda_v
            return GateState.WARMUP

        # Peak update and cooling logic (post-warmup only)
        if self._in_pass:
            # PASS: update peak normally; clear any lingering cooling state
            self._fail_start_ts = None
            self._cooling_active = False
            if self._lambda_v > self._lambda_v_peak:
                self._lambda_v_peak = self._lambda_v
        elif self.m_cool_sec > 0:
            # FAIL with cooling enabled: manage the fail timer and peak decay
            if self._fail_start_ts is None:
                self._fail_start_ts = timestamp
            fail_duration = timestamp - self._fail_start_ts
            if not self._cooling_active and fail_duration >= self.m_cool_sec:
                self._cooling_active = True
                self._peak_at_cool_start = self._lambda_v_peak
                self._cool_start_ts = timestamp
            if self._cooling_active:
                cool_elapsed = timestamp - self._cool_start_ts
                self._lambda_v_peak = self._peak_at_cool_start * math.exp(
                    -LN2 * cool_elapsed / self.tau_cool_sec
                )
            else:
                # Cooling not yet active — peak updates normally
                # (In FAIL, λ_V < p_open * peak ≤ peak, so this is only
                # reachable when building from zero on the very first tick.)
                if self._lambda_v > self._lambda_v_peak:
                    self._lambda_v_peak = self._lambda_v
        else:
            # FAIL with cooling disabled: update peak normally
            if self._lambda_v > self._lambda_v_peak:
                self._lambda_v_peak = self._lambda_v

        # Asymmetric hysteresis: FAIL→PASS at p_open, PASS→FAIL at p_close.
        # Symmetric when p_open == p_close (reproduces original behavior exactly).
        if self._in_pass:
            if self._lambda_v < self.p_close * self._lambda_v_peak:
                self._in_pass = False
                if self.m_cool_sec > 0:
                    self._fail_start_ts = timestamp
                    self._cooling_active = False
        else:
            if self._lambda_v >= self.p_open * self._lambda_v_peak:
                self._in_pass = True
                self._fail_start_ts = None
                self._cooling_active = False
        return GateState.PASS if self._in_pass else GateState.FAIL

    def _update_cusum(
        self,
        wji: Optional[float],
        wji_background: float,
        timestamp: float,
        is_halted: bool,
    ) -> GateState:
        """
        One-sided upper CUSUM branch (Phase CPD-1).

        Pipeline per tick (post-warmup):
            wji_log   = log(WJI / WJI_background)        # log-ratio transform
            deviation = wji_log / sigma_log              # standardise (H0 mean = 0)
            S_up      = max(0, S_up_prev + deviation - k) # accumulate, one-sided floor

        State (natural hysteresis from the accumulator):
            PASS  when  S_up > h
            FAIL  when  S_up <= 0          (evidence fully drained)
            hold        0 < S_up <= h      (keep prior PASS/FAIL)

        `caller passes the pre-computed (locked) WJI signal; wji_background defaults to
        1.0 (Cooper Option A — WJI is already background-normalised and rests at ≈ 1.0,
        so wji_log rests at ≈ 0 and no mean-subtraction is needed). sigma_log is
        estimated once from the warmup window and is then a fixed per-event constant.
        S_up resets to 0 only at event start (activate()), never between PASS/FAIL
        cycles — a partial drain lets the gate re-open faster on a continuation burst.
        """
        # Halt guard (T5b): a halted tick must leave S_up untouched. On the active-
        # seconds axis halt ticks are already removed, so this is belt-and-braces for
        # any raw-axis caller. CUSUM has no dt term, so "holding" is simply skipping.
        if is_halted:
            return GateState.PASS if self._in_pass else GateState.FAIL

        time_since_event = timestamp - self._t_event

        # Pre-event ticks (a full-trace replay feeds ticks before T_event): the gate is
        # not yet meaningful and must NOT collect them into the warmup sample — sigma_log
        # is estimated strictly on the post-event warmup window [T_event, T_event+warmup).
        if time_since_event < 0.0:
            return GateState.WARMUP

        # Zero-background / undefined-log guard: log needs WJI > 0 and background > 0.
        # On failure, contribute nothing to S_up and return the prior state.
        if wji is None or wji <= 0.0 or wji_background <= 0.0:
            if time_since_event < self.warmup_seconds:
                return GateState.WARMUP
            return GateState.PASS if self._in_pass else GateState.FAIL

        wji_log = math.log(wji / wji_background)

        # ── WARMUP: collect log-ratios for sigma_log; gate cannot open ──
        if time_since_event < self.warmup_seconds:
            self._warmup_logs.append(wji_log)
            return GateState.WARMUP

        # ── Warmup exit: finalise sigma_log once (fixed for the rest of the event) ──
        if not self._sigma_finalized:
            n = len(self._warmup_logs)
            if n >= 20:
                mean = sum(self._warmup_logs) / n
                var = sum((x - mean) ** 2 for x in self._warmup_logs) / (n - 1)
                sigma = math.sqrt(var) if var > 0.0 else 0.0
                self._sigma_log = sigma if sigma > 0.0 else self.sigma_log_fallback
            else:
                self._sigma_log = self.sigma_log_fallback
                log.info(
                    "cusum: warmup n=%d < 20 obs; using sigma_log fallback=%.4f",
                    n, self.sigma_log_fallback,
                )
            self._sigma_finalized = True

        # ── Accumulate evidence ──
        # deviation: how many rest-noise sigmas this tick sits above H0 (0).
        deviation = wji_log / self._sigma_log
        # k (slack): subtract so rest-level deviations do not accumulate. max(0, …)
        # is the one-sided CUSUM floor — evidence drains toward 0 but never banks
        # negative credit, so a return to background empties S_up (the exit signal).
        self._s_up = max(0.0, self._s_up + deviation - self.k)

        # ── State transition (hysteresis band 0 < S_up <= h holds prior state) ──
        if self._s_up > self.h:
            self._in_pass = True
        elif self._s_up <= 0.0:
            self._in_pass = False

        self._last_cusum_debug = {
            "wji": wji, "wji_background": wji_background, "wji_log": wji_log,
            "sigma_log": self._sigma_log, "deviation": deviation,
            "s_up": self._s_up, "k": self.k, "h": self.h, "in_pass": self._in_pass,
        }
        return GateState.PASS if self._in_pass else GateState.FAIL

    def _update_bocpd(
        self,
        wji: Optional[float],
        wji_background: float,
        timestamp: float,
        is_halted: bool,
    ) -> GateState:
        """
        Directional surge-aware Bayesian Online Changepoint Detection (Phase CPD-BOCPD).

        Operates on WJI_log(t) = log(WJI / WJI_background) (Option A: WJI_background ≡ 1.0,
        so WJI_log = log(WJI), resting at ≈ 0 = the H0 background mean).

        Model (standard Adams & MacKay 2007 with a Normal-Normal conjugate UPM):
            observations  x_t ~ N(mu_run, sigma_log^2)     sigma_log^2 KNOWN (per-event warmup)
            run mean      mu_run ~ N(0, prior_mean_std^2)   conjugate prior, mean 0 (= H0)
        Each run-length hypothesis r tracks sufficient stats (count_r, sum_r) over the r
        observations in that run. Posterior over its mean:
            denom_r   = 1/prior_mean_std^2 + count_r / sigma_log^2
            mu_post_r = (sum_r / sigma_log^2) / denom_r        # prior mean 0
            var_post_r= 1 / denom_r
        and the posterior predictive for the new x_t is N(mu_post_r, sigma_log^2 + var_post_r).
        This run-length dependence is what makes the posterior informative (the literal CPD-
        BOCPD spec used a run-length-INDEPENDENT predictive, which is degenerate — see the
        phase findings; Cooper approved this directional fix).

        Recursion per post-warmup tick (Adams-MacKay message passing):
            msg[r]   = R[r] * pred[r]
            R_new[0] = lambda_h * sum_r msg[r]                 # changepoint at t
            R_new[r+1] = (1 - lambda_h) * msg[r]               # run grows (absorbs x_t)
            R_new /= sum(R_new)
        Sufficient stats grow with the run: (count, sum) for run r -> r+1 become
        (count_r + 1, sum_r + x_t); the fresh run length 0 starts at (0, 0).

        Directional regime score (Cooper Option 2 — surge-aware, mirrors CPD-1's one-sided
        CUSUM):
            P_regime(t) = sum_{r>0} R_new[r] * 1[ mu_post_r(new) > dir_thresh ]
            dir_thresh  = dir_thresh_mult * sigma_log
        i.e. only run-length mass whose posterior mean is ELEVATED (an ongoing *up* regime)
        counts. Background runs (mu_post ≈ 0) and the changepoint bin (r=0) contribute 0, so
        the gate is long-only directional, unlike a vanilla non-directional BOCPD.

        Gate state (fixed 0.10 hysteresis gap, p_exit = p_enter - 0.10):
            PASS  when  P_regime >= p_enter
            FAIL  when  P_regime <  p_exit
            hold        p_exit <= P_regime < p_enter

        Run-length cap: arrays are capped at max_run_length (default 600). On overflow the
        mass that would land at run length (cap+1) is folded back into the cap bin; the cap
        bin keeps its own sufficient stats (a single absorbing long-run) — an approximation
        whose error is negligible because by run length 600 the posterior mean has long
        converged. sigma_log estimation reuses the cusum warmup machinery exactly.

        Halt handling: a halted tick freezes the posterior (R/count/sum untouched) and
        returns the prior state. On resume the next tick updates normally.
        """
        # Halt guard: freeze the posterior across a halt (belt-and-braces for raw-axis
        # callers; on the active-seconds axis halt ticks are already removed).
        if is_halted:
            return GateState.PASS if self._in_pass else GateState.FAIL

        time_since_event = timestamp - self._t_event

        # Pre-event ticks must NOT enter the warmup sigma sample.
        if time_since_event < 0.0:
            return GateState.WARMUP

        # Undefined-log guard: contribute nothing, hold prior state.
        if wji is None or wji <= 0.0 or wji_background <= 0.0:
            if time_since_event < self.warmup_seconds:
                return GateState.WARMUP
            return GateState.PASS if self._in_pass else GateState.FAIL

        wji_log = math.log(wji / wji_background)

        # ── WARMUP: collect log-ratios for sigma_log; gate cannot open ──
        if time_since_event < self.warmup_seconds:
            self._warmup_logs.append(wji_log)
            return GateState.WARMUP

        # ── Warmup exit: finalise sigma_log once, then seed the posterior ──
        if not self._sigma_finalized:
            n = len(self._warmup_logs)
            if n >= 20:
                mean = sum(self._warmup_logs) / n
                var = sum((v - mean) ** 2 for v in self._warmup_logs) / (n - 1)
                sigma = math.sqrt(var) if var > 0.0 else 0.0
                self._sigma_log = sigma if sigma > 0.0 else self.sigma_log_fallback
            else:
                self._sigma_log = self.sigma_log_fallback
                log.info(
                    "bocpd: warmup n=%d < 20 obs; using sigma_log fallback=%.4f",
                    n, self.sigma_log_fallback,
                )
            self._sigma_finalized = True

        if not self._bo_initialized:
            # R = [1.0] at run length 0 (a potential changepoint boundary entering the
            # active window); sufficient stats empty (prior only).
            self._bo_R = np.array([1.0], dtype=np.float64)
            self._bo_count = np.array([0.0], dtype=np.float64)
            self._bo_sum = np.array([0.0], dtype=np.float64)
            self._bo_len = 1
            self._bo_initialized = True

        sigma2 = self._sigma_log * self._sigma_log
        inv_s0_2 = 1.0 / (self.prior_mean_std * self.prior_mean_std)
        lh = self.lambda_h
        cap1 = self.max_run_length + 1   # max array length (run lengths 0..max_run_length)

        L = self._bo_len
        R = self._bo_R[:L]
        cnt = self._bo_count[:L]
        sm = self._bo_sum[:L]

        # Predictive of x_t under each current run-length hypothesis (uses the r prior
        # observations already absorbed into that run; r=0 = prior predictive).
        denom = inv_s0_2 + cnt / sigma2
        mu_post = (sm / sigma2) / denom
        pred_var = sigma2 + 1.0 / denom
        pred = np.exp(-0.5 * (wji_log - mu_post) ** 2 / pred_var) / np.sqrt(2.0 * math.pi * pred_var)

        msg = R * pred
        total = float(msg.sum())
        if not math.isfinite(total) or total <= 0.0:
            # Numerical underflow (x_t implausible under every hypothesis): freeze and hold.
            self._last_bocpd_debug = {
                "wji": wji, "wji_background": wji_background, "wji_log": wji_log,
                "sigma_log": self._sigma_log, "p_regime": self._p_regime,
                "p_enter": self.p_enter, "p_exit": self.p_exit,
                "r0": float(self._bo_R[0]) if self._bo_R is not None else None,
                "underflow": True, "in_pass": self._in_pass,
            }
            return GateState.PASS if self._in_pass else GateState.FAIL

        cp = lh * total                 # changepoint mass -> new run length 0
        growth = (1.0 - lh) * msg       # run r -> r+1 (absorbs x_t)

        if L < cap1:
            new_len = L + 1
            R_new = np.empty(new_len, dtype=np.float64)
            cnt_new = np.empty(new_len, dtype=np.float64)
            sm_new = np.empty(new_len, dtype=np.float64)
            R_new[0] = cp
            R_new[1:] = growth
            cnt_new[0] = 0.0
            cnt_new[1:] = cnt + 1.0
            sm_new[0] = 0.0
            sm_new[1:] = sm + wji_log
        else:
            # At the cap: growth would push old index cap -> cap+1. Fold it into the cap
            # bin (mass preserved). The cap bin keeps its own absorbing-run stats + x_t.
            new_len = cap1
            R_new = np.empty(new_len, dtype=np.float64)
            cnt_new = np.empty(new_len, dtype=np.float64)
            sm_new = np.empty(new_len, dtype=np.float64)
            R_new[0] = cp
            R_new[1:cap1 - 1] = growth[0:cap1 - 2]
            R_new[cap1 - 1] = growth[cap1 - 2] + growth[cap1 - 1]   # fold cap and cap+1
            cnt_new[0] = 0.0
            cnt_new[1:cap1 - 1] = cnt[0:cap1 - 2] + 1.0
            cnt_new[cap1 - 1] = cnt[cap1 - 1] + 1.0
            sm_new[0] = 0.0
            sm_new[1:cap1 - 1] = sm[0:cap1 - 2] + wji_log
            sm_new[cap1 - 1] = sm[cap1 - 1] + wji_log

        R_new /= total   # cp + sum(growth) == total (folding preserves the sum)

        # Directional surge-aware regime score on the UPDATED sufficient stats.
        dir_thresh = self.dir_thresh_mult * self._sigma_log
        denom_new = inv_s0_2 + cnt_new / sigma2
        mu_post_new = (sm_new / sigma2) / denom_new
        elevated = mu_post_new > dir_thresh
        # Exclude run length 0 (changepoint bin) per the directional spec.
        p_regime = float(np.dot(R_new[1:], elevated[1:].astype(np.float64)))

        self._bo_R = R_new
        self._bo_count = cnt_new
        self._bo_sum = sm_new
        self._bo_len = new_len
        self._p_regime = p_regime

        # State transition (hysteresis band p_exit <= P_regime < p_enter holds prior state).
        if p_regime >= self.p_enter:
            self._in_pass = True
        elif p_regime < self.p_exit:
            self._in_pass = False

        self._last_bocpd_debug = {
            "wji": wji, "wji_background": wji_background, "wji_log": wji_log,
            "sigma_log": self._sigma_log, "p_regime": p_regime,
            "p_enter": self.p_enter, "p_exit": self.p_exit,
            "r0": float(R_new[0]), "run_len_mode": int(np.argmax(R_new)),
            "dir_thresh": dir_thresh, "in_pass": self._in_pass,
        }
        return GateState.PASS if self._in_pass else GateState.FAIL

    def reset(self) -> None:
        """
        Reset for continuation events or new session.

        Clears peak, lambda_V, and T_event. Call activate() again when
        the new session's T_event is established.
        """
        self._lambda_v = 0.0
        self._lambda_v_peak = 0.0
        self._t_event = None
        self._last_timestamp = None
        self._active = False
        self._in_pass = False
        self._fail_start_ts = None
        self._cooling_active = False
        self._peak_at_cool_start = 0.0
        self._cool_start_ts = None
        self._peak_wji = 0.0
        self._lambda_buy_slow = 0.0
        self._lambda_v_ref = 1.0
        self._mu_buy_ref = 1.0
        self._thin_guard_count = 0
        self._thin_guard_total = 0
        self._last_bg_debug = {}
        self._s_up = 0.0
        self._sigma_log = None
        self._warmup_logs = []
        self._sigma_finalized = False
        self._last_cusum_debug = {}
        self._bo_R = None
        self._bo_count = None
        self._bo_sum = None
        self._bo_len = 0
        self._bo_initialized = False
        self._p_regime = 0.0
        self._last_bocpd_debug = {}
