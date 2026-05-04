"""
Trade-based OFI with two-step microprice classifier.

Self-contained -- can be called on any trade stream, not just mom_db.
Dependencies: None. Config loaded from config/lee_ready_thresholds.json.

Two-step classifier:
  Step 1: Standard Lee-Ready vs. mid with ambiguity band.
  Step 2: For ambiguous prints, compare vs. microprice with half-spread margin.
  See math_spec section 13 for the non-obvious convention.

OFI normalization uses time-averaged spread over the same 10s window (not
instantaneous spread). See math_spec section 7.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numba import njit


_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
_NS_PER_SECOND = 1_000_000_000


# -- Result dataclass ------------------------------------------------------


@dataclass
class OFIResult:
    """Result from compute_trade_ofi."""

    sides: np.ndarray        # int8: +1 buy, -1 sell, 0 ambiguous
    ofi_gate: np.ndarray     # float64: raw signed volume in trailing window
    ofi_norm: np.ndarray     # float64: spread-normalized OFI
    q_bar: float             # rolling mean trade size at end of sequence
    ambiguity_rate: float    # fraction still ambiguous after both steps
    step2_activation: float  # fraction of total trades classified by Step 2
    n_trades: int


# -- Scalar classifier (hot path / live) -----------------------------------


@njit(cache=True)
def classify_trade(
    price: float,
    bid: float,
    ask: float,
    bid_sz: float,
    ask_sz: float,
    ambiguity_thresh: float,
    step2_margin: float = 0.5,
) -> int:
    """Classify a single trade using the two-step microprice classifier.

    Step 1: price vs. mid. If |price - mid| > ambiguity_thresh * spread, classify.
    Step 2: For ambiguous, price vs. microprice with margin * half_spread.

    Returns: +1 (buy), -1 (sell), 0 (ambiguous).
    """
    if bid <= 0.0 or ask <= 0.0 or ask <= bid:
        return 0

    mid = (bid + ask) / 2.0
    spread = ask - bid
    half_spread = spread / 2.0

    # Step 1: Lee-Ready vs mid
    if price > mid + ambiguity_thresh * spread:
        return 1
    if price < mid - ambiguity_thresh * spread:
        return -1

    # Step 2: vs microprice
    total_sz = bid_sz + ask_sz
    if total_sz > 0.0:
        microprice = (ask * bid_sz + bid * ask_sz) / total_sz
    else:
        microprice = mid

    if price > microprice + step2_margin * half_spread:
        return 1
    if price < microprice - step2_margin * half_spread:
        return -1

    return 0


# -- Batch classifier (calibration) ---------------------------------------


@njit(cache=True)
def _classify_trades_batch(
    trade_prices: np.ndarray,       # float64
    trade_timestamps: np.ndarray,   # int64 nanoseconds
    quote_bid_prices: np.ndarray,   # float64
    quote_ask_prices: np.ndarray,   # float64
    quote_bid_sizes: np.ndarray,    # float64
    quote_ask_sizes: np.ndarray,    # float64
    quote_timestamps: np.ndarray,   # int64 nanoseconds
    step1_ambiguity_thresh: float,
    step2_margin: float,
):
    """Classify all trades using two-step classifier.

    Advances a quote pointer to find the prevailing quote at each trade time.

    Returns: (sides int8[], n_step2 int, n_ambiguous int)
    """
    n = len(trade_prices)
    nq = len(quote_timestamps)
    sides = np.empty(n, dtype=np.int8)
    n_step2 = 0
    n_ambiguous = 0
    q_idx = 0

    for i in range(n):
        # Advance to most recent quote at or before this trade
        while q_idx < nq - 1 and quote_timestamps[q_idx + 1] <= trade_timestamps[i]:
            q_idx += 1

        if q_idx >= nq or quote_timestamps[q_idx] > trade_timestamps[i]:
            sides[i] = 0
            n_ambiguous += 1
            continue

        bid = quote_bid_prices[q_idx]
        ask = quote_ask_prices[q_idx]
        bid_sz = quote_bid_sizes[q_idx]
        ask_sz = quote_ask_sizes[q_idx]

        # Guard: invalid quotes
        if bid <= 0.0 or ask <= 0.0 or ask <= bid:
            sides[i] = 0
            n_ambiguous += 1
            continue

        mid = (bid + ask) / 2.0
        spread = ask - bid
        half_spread = spread / 2.0
        price = trade_prices[i]

        # Step 1: Lee-Ready vs mid
        if price > mid + step1_ambiguity_thresh * spread:
            sides[i] = 1
            continue
        if price < mid - step1_ambiguity_thresh * spread:
            sides[i] = -1
            continue

        # Step 2: vs microprice
        total_sz = bid_sz + ask_sz
        if total_sz > 0.0:
            microprice = (ask * bid_sz + bid * ask_sz) / total_sz
        else:
            microprice = mid

        if price > microprice + step2_margin * half_spread:
            sides[i] = 1
            n_step2 += 1
        elif price < microprice - step2_margin * half_spread:
            sides[i] = -1
            n_step2 += 1
        else:
            sides[i] = 0
            n_ambiguous += 1

    return sides, n_step2, n_ambiguous


# -- OFI computation (sliding window) -------------------------------------


@njit(cache=True)
def _compute_ofi_arrays(
    trade_ts: np.ndarray,       # int64 nanoseconds
    trade_sizes: np.ndarray,    # float64
    sides: np.ndarray,          # int8
    quote_ts: np.ndarray,       # int64 nanoseconds
    quote_spreads: np.ndarray,  # float64 (ask - bid), pre-computed
    window_ns: np.int64,
    q_bar_min_trades: int,
    q_bar_fallback: float,
    q_bar_winsorize_cap: float,  # 0.0 = no cap
):
    """Compute OFI_gate and OFI_norm at each trade time.

    Uses efficient sliding windows for both trades and quote spreads.
    O(N_trades + N_quotes) amortized.

    OFI_gate(t) = sum of s_j * v_j for trades in [t - window, t]
    OFI_norm(t) = OFI_gate(t) / (avg_spread_window * Q_bar)

    Returns: (ofi_gate float64[], ofi_norm float64[], final_q_bar float)
    """
    n = len(trade_ts)
    nq = len(quote_ts)
    ofi_gate = np.zeros(n, dtype=np.float64)
    ofi_norm = np.zeros(n, dtype=np.float64)

    # Sliding window: OFI_gate
    trade_win_start = 0
    gate_sum = 0.0

    # Sliding window: quote spread averaging
    quote_win_start = 0
    quote_win_end = 0
    spread_sum = 0.0
    spread_count = 0

    # Rolling Q_bar (mean trade size since session start)
    cum_size = 0.0
    n_counted = 0

    for i in range(n):
        t_i = trade_ts[i]
        t_win = t_i - window_ns

        # --- Add current trade to gate sum ---
        if sides[i] != 0:
            gate_sum += float(sides[i]) * trade_sizes[i]

        # --- Remove expired trades ---
        while trade_win_start < i and trade_ts[trade_win_start] < t_win:
            if sides[trade_win_start] != 0:
                gate_sum -= float(sides[trade_win_start]) * trade_sizes[trade_win_start]
            trade_win_start += 1

        ofi_gate[i] = gate_sum

        # --- Q_bar: rolling mean trade size ---
        n_counted += 1
        cum_size += trade_sizes[i]
        if n_counted >= q_bar_min_trades:
            q_bar = cum_size / float(n_counted)
        else:
            q_bar = q_bar_fallback

        if q_bar_winsorize_cap > 0.0 and q_bar > q_bar_winsorize_cap:
            q_bar = q_bar_winsorize_cap

        # --- Quote spread sliding window ---
        # Remove quotes that fell out of window
        while quote_win_start < quote_win_end and quote_ts[quote_win_start] < t_win:
            s = quote_spreads[quote_win_start]
            if s > 0.0:
                spread_sum -= s
                spread_count -= 1
            quote_win_start += 1

        # Add new quotes up to t_i
        while quote_win_end < nq and quote_ts[quote_win_end] <= t_i:
            s = quote_spreads[quote_win_end]
            if s > 0.0:
                spread_sum += s
                spread_count += 1
            quote_win_end += 1

        # --- OFI_norm ---
        if spread_count > 0 and q_bar > 0.0:
            avg_spread = spread_sum / float(spread_count)
            if avg_spread > 0.0:
                ofi_norm[i] = gate_sum / (avg_spread * q_bar)

    final_q_bar = cum_size / float(max(n_counted, 1))
    return ofi_gate, ofi_norm, final_q_bar


# -- Config loading --------------------------------------------------------


def _load_thresholds(session_hour=None):
    """Load classifier thresholds from config/lee_ready_thresholds.json.

    Falls back to defaults (0.10, 0.5) if config not found.
    """
    config_path = _CONFIG_DIR / "lee_ready_thresholds.json"
    if config_path.exists():
        with open(config_path) as f:
            cfg = json.load(f)

        if session_hour is not None:
            if session_hour < 9.5:
                key = "pre_market"
            elif session_hour >= 16.0:
                key = "post_market"
            else:
                key = "regular_hours"
        else:
            key = "regular_hours"

        entry = cfg.get(key, cfg.get("regular_hours", {}))
        return (
            entry.get("step1_ambiguity_thresh", 0.10),
            entry.get("step2_margin", 0.5),
        )

    return 0.10, 0.5


# -- Main entry point ------------------------------------------------------


def compute_trade_ofi(
    trade_timestamps: np.ndarray,
    trade_prices: np.ndarray,
    trade_sizes: np.ndarray,
    quote_timestamps: np.ndarray,
    quote_bid_prices: np.ndarray,
    quote_ask_prices: np.ndarray,
    quote_bid_sizes: np.ndarray,
    quote_ask_sizes: np.ndarray,
    window_sec: float = 10.0,
    ambiguity_thresh: float | None = None,
    session_hour: float | None = None,
    q_bar_fallback: float = 100.0,
    q_bar_min_trades: int = 50,
    q_bar_winsorize_cap: float = 0.0,
) -> OFIResult:
    """Compute trade-based OFI with two-step classifier.

    Args:
        trade_timestamps: int64 nanosecond timestamps (sorted)
        trade_prices: float64 trade prices
        trade_sizes: int64 trade sizes in shares
        quote_timestamps: int64 nanosecond timestamps (sorted)
        quote_bid_prices: float64 bid prices at each quote update
        quote_ask_prices: float64 ask prices at each quote update
        quote_bid_sizes: bid sizes at each quote update
        quote_ask_sizes: ask sizes at each quote update
        window_sec: OFI trailing window in seconds (default 10)
        ambiguity_thresh: Step 1 threshold (fraction of spread); loaded from
            config if None
        session_hour: hour of day (ET) for session-specific thresholds
        q_bar_fallback: fallback Q_bar when < min_trades (per-tier median)
        q_bar_min_trades: minimum trades before using rolling Q_bar (default 50)
        q_bar_winsorize_cap: cap Q_bar at this value (0.0 = no cap). Set to
            95th percentile of per-tier Q_bar from training catalog.

    Returns:
        OFIResult with per-trade sides, ofi_gate, ofi_norm, and diagnostics.
    """
    n = len(trade_timestamps)
    if n == 0:
        return OFIResult(
            sides=np.array([], dtype=np.int8),
            ofi_gate=np.array([], dtype=np.float64),
            ofi_norm=np.array([], dtype=np.float64),
            q_bar=0.0,
            ambiguity_rate=0.0,
            step2_activation=0.0,
            n_trades=0,
        )

    # Load thresholds
    if ambiguity_thresh is None:
        step1_thresh, step2_margin = _load_thresholds(session_hour)
    else:
        step1_thresh = ambiguity_thresh
        _, step2_margin = _load_thresholds(session_hour)

    # Classify trades
    sides, n_step2, n_ambiguous = _classify_trades_batch(
        trade_prices.astype(np.float64),
        trade_timestamps.astype(np.int64),
        quote_bid_prices.astype(np.float64),
        quote_ask_prices.astype(np.float64),
        quote_bid_sizes.astype(np.float64),
        quote_ask_sizes.astype(np.float64),
        quote_timestamps.astype(np.int64),
        float(step1_thresh),
        float(step2_margin),
    )

    # Pre-compute quote spreads
    spreads = (
        quote_ask_prices.astype(np.float64) - quote_bid_prices.astype(np.float64)
    )

    # Compute OFI arrays
    window_ns = np.int64(int(window_sec * _NS_PER_SECOND))
    ofi_gate, ofi_norm, final_q_bar = _compute_ofi_arrays(
        trade_timestamps.astype(np.int64),
        trade_sizes.astype(np.float64),
        sides,
        quote_timestamps.astype(np.int64),
        spreads,
        window_ns,
        int(q_bar_min_trades),
        float(q_bar_fallback),
        float(q_bar_winsorize_cap),
    )

    return OFIResult(
        sides=sides,
        ofi_gate=ofi_gate,
        ofi_norm=ofi_norm,
        q_bar=final_q_bar,
        ambiguity_rate=n_ambiguous / n if n > 0 else 0.0,
        step2_activation=n_step2 / n if n > 0 else 0.0,
        n_trades=n,
    )


# -- Post-entry OFI (EXIT_5) -----------------------------------------------


def compute_post_entry_ofi_norm(signed_volumes: list) -> float:
    """Compute normalized net signed volume for EXIT_5 adverse flow detection.

    Args:
        signed_volumes: list of signed volume values since entry.
            Each value is +size (BUY) or -size (SELL) for that tick.
            Ambiguous trades (side=0) contribute 0 signed volume but count
            toward total volume with abs(size).

    Returns:
        float in [-1, 1]: net signed volume / total volume
        float('nan') if fewer than 3 trades or total volume is zero.
        NaN is never a signal -- caller must not fire EXIT_5 on NaN.
    """
    if len(signed_volumes) < 3:
        return float("nan")

    net_signed = 0.0
    total_vol = 0.0
    for sv in signed_volumes:
        net_signed += sv
        total_vol += abs(sv)

    if total_vol == 0.0:
        return float("nan")

    return net_signed / total_vol
