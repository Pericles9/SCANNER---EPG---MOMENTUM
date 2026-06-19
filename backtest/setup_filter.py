"""
Tradeable Setup Filter — core/filters/setup_filter.py

Universe gate.  Continuously determines whether a stock is liquid and
naturally active.  Applied in real time during live trading and
retrospectively to the historical catalog.

Implements 4 exponential-forgetting signals on 1-minute OHLCV bars:
  1. RangeScore  — is price moving?
  2. VolScore    — are participants present?
  3. ThinScore   — are a few trades moving price?
  4. BodyScore   — is there directional commitment?

Composite:  Q(t) = geometric mean of all four, smoothed by rho_fast.
Qualification: Q_tilde >= 0.65 sustained for 15 minutes.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numba import njit

NS_PER_SECOND = 1_000_000_000

# ── Constants ───────────────────────────────────────────────────────────

RHO_SLOW = 0.985       # background prior forgetting
RHO_FAST = 0.90        # composite state tracker
C_RANGE = 0.60         # range floor multiplier
C_VOL = 0.30           # volume floor multiplier
C_THIN = 2.50          # thinness ceiling multiplier
C_BODY = 0.40          # body conviction floor multiplier
Q_THRESHOLD = 0.65     # composite qualification threshold
SUSTAIN_BARS = 15      # minutes of sustained qualification
WARMUP_BARS = 65       # ~1/(1-rho_slow) before priors meaningful
WARMUP_THRESHOLD = 0.75  # higher bar during warm-up
BAR_SECONDS = 60       # 1-minute bars

PSI_LOOKBACK_DAYS = 3  # data integrity check lookback


# ── 1-minute bar construction from trades ───────────────────────────────

@njit(cache=True)
def _build_1min_bars(
    timestamps: np.ndarray,   # int64 ns
    prices: np.ndarray,       # float64
    sizes: np.ndarray,        # int64
    session_start_ns: int,
    session_end_ns: int,
):
    """Build 1-minute OHLCV + dollar volume bars from sorted trade ticks.

    Returns arrays of shape (n_bars,) for:
      open, high, low, close, volume, dollar_volume, bar_start_ns
    Empty bars (no trades) are omitted.
    """
    if len(timestamps) == 0:
        empty_f = np.empty(0, dtype=np.float64)
        empty_i = np.empty(0, dtype=np.int64)
        return empty_f, empty_f, empty_f, empty_f, empty_i, empty_f, empty_i

    bar_ns = 60 * NS_PER_SECOND  # 1 minute

    # Determine number of possible bars
    n_possible = int((session_end_ns - session_start_ns) / bar_ns) + 1

    # Pre-allocate output arrays
    opens = np.empty(n_possible, dtype=np.float64)
    highs = np.empty(n_possible, dtype=np.float64)
    lows = np.empty(n_possible, dtype=np.float64)
    closes = np.empty(n_possible, dtype=np.float64)
    volumes = np.empty(n_possible, dtype=np.int64)
    dvols = np.empty(n_possible, dtype=np.float64)
    bar_starts = np.empty(n_possible, dtype=np.int64)

    n_bars = 0

    # Current bar state
    cur_bar_start = -1
    cur_open = 0.0
    cur_high = -1e30
    cur_low = 1e30
    cur_close = 0.0
    cur_vol = np.int64(0)
    cur_dv = 0.0

    for i in range(len(timestamps)):
        ts = timestamps[i]
        if ts < session_start_ns or ts > session_end_ns:
            continue

        bar_idx = int((ts - session_start_ns) / bar_ns)
        bar_start = session_start_ns + bar_idx * bar_ns

        if bar_start != cur_bar_start:
            # Flush previous bar
            if cur_bar_start >= 0 and cur_vol > 0:
                opens[n_bars] = cur_open
                highs[n_bars] = cur_high
                lows[n_bars] = cur_low
                closes[n_bars] = cur_close
                volumes[n_bars] = cur_vol
                dvols[n_bars] = cur_dv
                bar_starts[n_bars] = cur_bar_start
                n_bars += 1

            cur_bar_start = bar_start
            cur_open = prices[i]
            cur_high = prices[i]
            cur_low = prices[i]
            cur_close = prices[i]
            cur_vol = np.int64(sizes[i])
            cur_dv = prices[i] * float(sizes[i])
        else:
            if prices[i] > cur_high:
                cur_high = prices[i]
            if prices[i] < cur_low:
                cur_low = prices[i]
            cur_close = prices[i]
            cur_vol += np.int64(sizes[i])
            cur_dv += prices[i] * float(sizes[i])

    # Flush last bar
    if cur_bar_start >= 0 and cur_vol > 0:
        opens[n_bars] = cur_open
        highs[n_bars] = cur_high
        lows[n_bars] = cur_low
        closes[n_bars] = cur_close
        volumes[n_bars] = cur_vol
        dvols[n_bars] = cur_dv
        bar_starts[n_bars] = cur_bar_start
        n_bars += 1

    return (
        opens[:n_bars],
        highs[:n_bars],
        lows[:n_bars],
        closes[:n_bars],
        volumes[:n_bars],
        dvols[:n_bars],
        bar_starts[:n_bars],
    )


# ── Core filter computation (JIT) ──────────────────────────────────────

@njit(cache=True)
def _compute_setup_signals(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    volumes: np.ndarray,
    dollar_volumes: np.ndarray,
    rho_fast: float = RHO_FAST,
):
    """Compute the 4 setup filter signals and composite Q trajectory.

    Returns:
      range_scores, vol_scores, thin_scores, body_scores,
      q_raw, q_tilde  — all shape (n_bars,)
    """
    n = len(opens)
    range_scores = np.empty(n, dtype=np.float64)
    vol_scores = np.empty(n, dtype=np.float64)
    thin_scores = np.empty(n, dtype=np.float64)
    body_scores = np.empty(n, dtype=np.float64)
    q_raw = np.empty(n, dtype=np.float64)
    q_tilde = np.empty(n, dtype=np.float64)

    # Running priors
    mu_r = 0.0
    mu_v = 0.0
    mu_tau = 0.0
    mu_b = 0.0
    qt = 0.5  # initial Q_tilde

    for i in range(n):
        h = highs[i]
        l = lows[i]
        c = closes[i]
        o = opens[i]
        v = float(volumes[i])
        dv = dollar_volumes[i]
        bar_range = h - l

        # --- Signal 1: Range ---
        r_t = bar_range / c if c > 0 else 0.0
        if i == 0:
            mu_r = r_t
        else:
            mu_r = RHO_SLOW * mu_r + (1.0 - RHO_SLOW) * r_t
        denom_r = mu_r * C_RANGE
        rs = min(r_t / denom_r, 1.0) if denom_r > 1e-15 else 1.0
        range_scores[i] = rs

        # --- Signal 2: Volume ---
        if i == 0:
            mu_v = v
        else:
            mu_v = RHO_SLOW * mu_v + (1.0 - RHO_SLOW) * v
        denom_v = mu_v * C_VOL
        vs = min(v / denom_v, 1.0) if denom_v > 1e-15 else 1.0
        vol_scores[i] = vs

        # --- Signal 3: Thinness ---
        # tau_t = (bar_range * close) / dollar_volume
        if dv > 0 and bar_range > 0:
            price_mid = (h + l) / 2.0  # P_t in spec
            tau_t = (bar_range * price_mid) / dv
        else:
            tau_t = mu_tau if mu_tau > 0 else 0.0  # spec: set tau_t = mu_tau when dv=0

        if i == 0:
            mu_tau = tau_t
        else:
            mu_tau = RHO_SLOW * mu_tau + (1.0 - RHO_SLOW) * tau_t

        denom_tau = mu_tau * C_THIN
        if denom_tau > 1e-15:
            ts_val = max(1.0 - tau_t / denom_tau, 0.0)
        else:
            ts_val = 1.0
        thin_scores[i] = ts_val

        # --- Signal 4: Body Conviction ---
        if bar_range > 1e-10:
            b_t = abs(c - o) / bar_range
        else:
            b_t = 0.0

        if i == 0:
            mu_b = b_t
        else:
            mu_b = RHO_SLOW * mu_b + (1.0 - RHO_SLOW) * b_t

        denom_b = mu_b * C_BODY
        bs = min(b_t / denom_b, 1.0) if denom_b > 1e-15 else 1.0
        body_scores[i] = bs

        # --- Composite ---
        product = rs * vs * ts_val * bs
        q = product ** 0.25 if product > 0 else 0.0
        q_raw[i] = q

        qt = rho_fast * qt + (1.0 - rho_fast) * q
        q_tilde[i] = qt

    return range_scores, vol_scores, thin_scores, body_scores, q_raw, q_tilde


@njit(cache=True)
def _check_sustained(q_tilde: np.ndarray, threshold: float, sustain_bars: int, warmup_bars: int, warmup_threshold: float):
    """Check if Q_tilde sustains above threshold for sustain_bars minutes.

    Returns:
      passes: bool — whether the event passes
      first_qualify_bar: int — first bar where sustained qualification starts (-1 if never)
      last_fail_bar: int — last bar where Q_tilde was below threshold and never recovered (-1 if never failed)
      min_sustained_q: float — minimum Q_tilde during the sustained window
    """
    n = len(q_tilde)
    if n < sustain_bars:
        return False, -1, -1, 0.0

    # Find the first stretch of sustain_bars consecutive bars above threshold
    consecutive = 0
    first_qualify = -1
    min_q_in_window = 1e30
    passes = False

    for i in range(n):
        # During warmup, use higher threshold
        thr = warmup_threshold if i < warmup_bars else threshold

        if q_tilde[i] >= thr:
            consecutive += 1
            if q_tilde[i] < min_q_in_window:
                min_q_in_window = q_tilde[i]
            if consecutive >= sustain_bars and not passes:
                first_qualify = i - sustain_bars + 1
                passes = True
        else:
            consecutive = 0
            min_q_in_window = 1e30

    # Find last fail bar (last time Q_tilde dropped below threshold and didn't recover)
    last_fail = -1
    for i in range(n - 1, -1, -1):
        if q_tilde[i] < threshold:
            last_fail = i
            break

    return passes, first_qualify, last_fail, min_q_in_window if passes else 0.0


# ── ψ data integrity check ─────────────────────────────────────────────

def check_psi(
    event_close: float,
    lookback_low: float,
) -> bool:
    """ψ verifies the stock actually gapped >50%.

    psi = (close - low_lookback) / low_lookback > 0.50
    """
    if lookback_low <= 0:
        return False
    psi = (event_close - lookback_low) / lookback_low
    return psi > 0.50


def compute_lookback_low(
    timestamps: np.ndarray,
    prices: np.ndarray,
    event_date_ns: int,
    lookback_days: int = PSI_LOOKBACK_DAYS,
) -> float:
    """Compute the low price over the prior N trading days before event date.

    Uses all trades with timestamp < event_date_ns - this captures
    the lookback context stored in the data files.
    """
    lookback_ns = lookback_days * 24 * 3600 * NS_PER_SECOND
    cutoff = event_date_ns - lookback_ns

    mask = (timestamps >= cutoff) & (timestamps < event_date_ns)
    if not np.any(mask):
        return 0.0
    return float(np.min(prices[mask]))


# ── Public API ──────────────────────────────────────────────────────────

@dataclass
class SetupFilterResult:
    """Result of running the setup filter on a single event."""
    passes: bool
    psi_passes: bool
    n_bars: int
    first_qualify_bar: int     # -1 if never qualified
    last_fail_bar: int         # -1 if never failed after qualifying
    min_sustained_q: float
    mean_q_tilde: float
    weakest_signal: str        # which signal was the bottleneck
    range_scores: np.ndarray = field(repr=False)
    vol_scores: np.ndarray = field(repr=False)
    thin_scores: np.ndarray = field(repr=False)
    body_scores: np.ndarray = field(repr=False)
    q_raw: np.ndarray = field(repr=False)
    q_tilde: np.ndarray = field(repr=False)


def _identify_weakest_signal(
    range_scores: np.ndarray,
    vol_scores: np.ndarray,
    thin_scores: np.ndarray,
    body_scores: np.ndarray,
) -> str:
    """Identify which signal was the weakest on average."""
    means = {
        "range": np.mean(range_scores),
        "volume": np.mean(vol_scores),
        "thinness": np.mean(thin_scores),
        "body": np.mean(body_scores),
    }
    return min(means, key=means.get)


def run_setup_filter(
    timestamps: np.ndarray,
    prices: np.ndarray,
    sizes: np.ndarray,
    session_start_ns: int,
    session_end_ns: int,
    lookback_low: float | None = None,
    event_close: float | None = None,
    rho_fast: float = RHO_FAST,
) -> SetupFilterResult:
    """Run the full setup filter on a single event.

    Parameters
    ----------
    timestamps : sorted int64 nanosecond trade timestamps
    prices : float64 trade prices
    sizes : int64 trade sizes
    session_start_ns : session start (4am ET) in nanoseconds
    session_end_ns : session end (8pm ET) in nanoseconds
    lookback_low : low price over prior 3 days (for ψ check). None skips ψ.
    event_close : close price on event day (for ψ check). None skips ψ.

    Returns
    -------
    SetupFilterResult with all signal trajectories and pass/fail decision.
    """
    # Build 1-minute bars
    opens, highs, lows, closes, volumes, dvols, bar_starts = _build_1min_bars(
        timestamps, prices, sizes, session_start_ns, session_end_ns
    )

    n_bars = len(opens)

    # ψ data integrity check
    psi_passes = True
    if lookback_low is not None and event_close is not None:
        psi_passes = check_psi(event_close, lookback_low)

    if n_bars < SUSTAIN_BARS:
        return SetupFilterResult(
            passes=False,
            psi_passes=psi_passes,
            n_bars=n_bars,
            first_qualify_bar=-1,
            last_fail_bar=-1,
            min_sustained_q=0.0,
            mean_q_tilde=0.0,
            weakest_signal="insufficient_bars",
            range_scores=np.array([], dtype=np.float64),
            vol_scores=np.array([], dtype=np.float64),
            thin_scores=np.array([], dtype=np.float64),
            body_scores=np.array([], dtype=np.float64),
            q_raw=np.array([], dtype=np.float64),
            q_tilde=np.array([], dtype=np.float64),
        )

    # Compute signals
    range_scores, vol_scores, thin_scores, body_scores, q_raw, q_tilde = (
        _compute_setup_signals(opens, highs, lows, closes, volumes, dvols, rho_fast)
    )

    # Check sustained qualification
    passes, first_qualify, last_fail, min_q = _check_sustained(
        q_tilde, Q_THRESHOLD, SUSTAIN_BARS, WARMUP_BARS, WARMUP_THRESHOLD
    )

    weakest = _identify_weakest_signal(range_scores, vol_scores, thin_scores, body_scores)

    return SetupFilterResult(
        passes=passes and psi_passes,
        psi_passes=psi_passes,
        n_bars=n_bars,
        first_qualify_bar=first_qualify,
        last_fail_bar=last_fail,
        min_sustained_q=min_q,
        mean_q_tilde=float(np.mean(q_tilde)),
        weakest_signal=weakest,
        range_scores=range_scores,
        vol_scores=vol_scores,
        thin_scores=thin_scores,
        body_scores=body_scores,
        q_raw=q_raw,
        q_tilde=q_tilde,
    )
