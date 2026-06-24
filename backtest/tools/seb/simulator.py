"""
Phase SEB — Causal entry simulator.

Implements causality rules A through F from the Phase SEB specification:
  A: Poll-spaced grid (20s default, from config or fallback 30s)
  B: todaysChangePerc uses official prior-day close (prevClose)
  C: 30% gate, one-shot per session (first cross only)
  D: armed_bar = max(candidate_bar, first_qualify_bar + SUSTAIN_BARS - 1)
  E: Per-bucket session VWAP (4am anchor, reset at 09:30 and 16:00 ET)
  F: {MOM} is hindsight — used only for labeling, never in decisions

Gate B assertions fire loudly if violated:
  armed_bar >= first_qualify_bar + SUSTAIN_BARS - 1
  entry_bar >= armed_bar
"""
from __future__ import annotations

import functools
import logging
import math
import sys
from pathlib import Path
from typing import Optional

import numpy as np

# Add backtest/ (for data.*, setup_filter) and project root (for Numba cache
# reconstruction, which needs `import backtest` to resolve) to sys.path.
_REPO_ROOT = Path(__file__).resolve().parents[2]      # …/backtest
_PROJECT_ROOT = _REPO_ROOT.parent                     # …/scanner-epg-momentum
for _p in (str(_PROJECT_ROOT), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from data.loaders.prev_close import get_prev_close as _get_prev_close  # noqa: E402

# Cache prevClose lookups — the loader hits DuckDB/parquet on every call by design;
# caching here avoids redundant reads for the same (ticker, date) across window sessions.
get_prev_close = functools.lru_cache(maxsize=None)(_get_prev_close)
from data.loaders.trades import _session_ns_bounds  # noqa: E402
from data.schemas.mom_db import NS_PER_SECOND  # noqa: E402
from setup_filter import (  # noqa: E402
    _build_1min_bars, run_setup_filter,
    SUSTAIN_BARS, Q_THRESHOLD, WARMUP_BARS, WARMUP_THRESHOLD,
)
from tools.seb.feed import SessionSpec, load_ticks_for_session  # noqa: E402

log = logging.getLogger(__name__)

# ── Session-time constants (offsets from 4am session start) ────────────
# 4am ET is session_start_ns.  All offsets are in nanoseconds from there.
_RTH_START_NS_OFFSET = int(5.5 * 3600 * NS_PER_SECOND)   # 09:30 ET
_RTH_END_NS_OFFSET   = int(12.0 * 3600 * NS_PER_SECOND)  # 16:00 ET

# ── Forward metrics knobs ───────────────────────────────────────────────
HORIZONS_MIN = [5, 15, 30, 60]          # MFE/MAE/return windows
RUNNER_THRESHOLD = 0.05                  # MFE(30m) >= 5% — UNVALIDATED HEURISTIC

# ── Gate A tolerance ────────────────────────────────────────────────────
GATE_A_REL_TOL_TIER1  = 0.40   # 40% relative — loose sanity check for catalog events
GATE_A_REL_TOL_TIER0  = 0.15   # 15% relative — tighter prevClose calibration check


# ── Session-bucket helper (local copy — avoids runner.py import) ────────

def _session_bucket(offset_from_4am_ns: int) -> str:
    """Return the session bucket for a nanosecond offset from 4am session start."""
    if offset_from_4am_ns < _RTH_START_NS_OFFSET:
        return "pre_market"
    if offset_from_4am_ns < _RTH_END_NS_OFFSET:
        return "regular_hours"
    return "post_market"


# ── Low-level helpers ───────────────────────────────────────────────────

def _price_at(ts_ns: int, timestamps: np.ndarray, prices: np.ndarray) -> float:
    """Last trade price at or before ts_ns.  NaN if no prior trade."""
    idx = int(np.searchsorted(timestamps, ts_ns + 1, side="left")) - 1
    return float(prices[idx]) if idx >= 0 else float("nan")


def _find_bar_idx(bar_starts: np.ndarray, ts_ns: int) -> int:
    """Index of the bar whose start <= ts_ns.  -1 if ts_ns is before all bars."""
    return int(np.searchsorted(bar_starts, ts_ns + 1, side="left")) - 1


def _build_poll_grid(
    session_start_ns: int,
    first_tick_ns: int,
    last_tick_ns: int,
    poll_interval_ns: int,
) -> list[int]:
    """Synthetic poll timestamps spaced by poll_interval_ns from session_start_ns.

    Only returns timestamps in [first_tick_ns, last_tick_ns] — no polls
    outside the window where we have actual trade data.
    """
    polls = []
    t = session_start_ns
    while t <= last_tick_ns:
        if t >= first_tick_ns:
            polls.append(int(t))
        t += poll_interval_ns
    return polls


def _compute_vwap_per_bar(
    bar_starts: np.ndarray,
    dvols: np.ndarray,
    volumes: np.ndarray,
    session_start_ns: int,
) -> np.ndarray:
    """Compute running per-bar VWAP with per-bucket resets at 09:30 and 16:00 ET.

    Uses dvols (dollar volume = sum(price*size)) and volumes for accurate VWAP.
    Resets at each bucket boundary (pre→RTH, RTH→post).
    Returns an array of VWAP values aligned to bar_starts.
    """
    n = len(bar_starts)
    vwap = np.zeros(n, dtype=np.float64)
    cumul_pv = 0.0
    cumul_v = 0.0
    prev_bucket: Optional[str] = None

    for i in range(n):
        offset_ns = int(bar_starts[i]) - session_start_ns
        bucket = _session_bucket(offset_ns)

        if prev_bucket is not None and bucket != prev_bucket:
            cumul_pv = 0.0
            cumul_v = 0.0

        prev_bucket = bucket
        cumul_pv += float(dvols[i])
        cumul_v += float(volumes[i])
        vwap[i] = cumul_pv / cumul_v if cumul_v > 0.0 else 0.0

    return vwap


def _compute_forward_metrics(
    entry_ts_ns: int,
    entry_price: float,
    timestamps: np.ndarray,
    prices: np.ndarray,
    session_end_ns: int,
) -> dict:
    """Compute MFE, MAE, and returns at HORIZONS_MIN windows and EOD, plus runner flag.

    All metrics use ticks strictly after entry_ts_ns (evaluation, not decision).
    entry_ts_ns is the close of the entry bar (= bar_start + 60s).
    """
    result: dict = {}
    mask_after = timestamps > entry_ts_ns

    for h in HORIZONS_MIN:
        h_ns = h * 60 * NS_PER_SECOND
        h_mask = mask_after & (timestamps <= entry_ts_ns + h_ns)
        if np.any(h_mask):
            hp = prices[h_mask]
            mfe = float((np.max(hp) - entry_price) / entry_price)
            mae = float((np.min(hp) - entry_price) / entry_price)
            ret = float((float(hp[-1]) - entry_price) / entry_price)
        else:
            mfe = mae = ret = 0.0
        result[f"mfe_{h}m"] = mfe
        result[f"mae_{h}m"] = mae
        result[f"ret_{h}m"] = ret

    # EOD return: last tick in session after entry
    eod_mask = mask_after & (timestamps <= session_end_ns)
    if np.any(eod_mask):
        result["eod_ret"] = float((float(prices[eod_mask][-1]) - entry_price) / entry_price)
    else:
        result["eod_ret"] = 0.0

    # Runner flag (UNVALIDATED HEURISTIC): MFE in 30-min window >= RUNNER_THRESHOLD
    result["is_runner"] = bool(result["mfe_30m"] >= RUNNER_THRESHOLD)

    return result


# ── Main simulator ──────────────────────────────────────────────────────

def simulate_session(
    spec: SessionSpec,
    poll_interval_s: float = 30.0,
) -> dict:
    """Run the causal entry simulator for one (ticker, date) session.

    Returns a dict with all fields for the entries parquet row.
    No data after the entry bar close is used in any decision.
    Forward metrics (MFE/MAE/returns) use post-entry data for evaluation only.

    Rules enforced:
      A — Polls spaced by poll_interval_s from session_start.
          Tier 0 uses recorded poll timestamps instead of the synthetic grid.
      B — todaysChangePerc reconstructed from get_prev_close() (Tier 1)
          or taken from the recorded scanner snapshot (Tier 0).
      C — One-shot: only the FIRST poll at >= 30% is the candidate.
      D — armed_bar = max(candidate_bar, first_qualify_bar + SUSTAIN_BARS - 1).
          Setup filter runs on ticks up to (and including) candidate_ts_ns.
      E — Entry = first bar close strictly above per-bucket session VWAP
          at or after armed_bar.
      F — mom_pct used only for labeling/bucketing, never in decisions.

    Gate B assertions:
      armed_bar >= first_qualify_bar + SUSTAIN_BARS - 1
      entry_bar >= armed_bar
    """
    base = {
        "tier": spec.tier,
        "ticker": spec.ticker,
        "date": spec.date,
        "mom_pct": spec.mom_pct,
        "scanner_quartile": spec.scanner_quartile,
        "is_event_day": spec.is_event_day,
    }

    # ── Load ticks ──────────────────────────────────────────────────────
    tick_data = load_ticks_for_session(spec.ticker, spec.date)
    if tick_data is None:
        return {**base, "no_entry_reason": "no_data"}

    timestamps, prices, sizes = tick_data

    # ── Session bounds ──────────────────────────────────────────────────
    session_start_ns, session_end_ns = _session_ns_bounds(spec.date)

    # Restrict to session window
    sess_mask = (timestamps >= session_start_ns) & (timestamps <= session_end_ns)
    timestamps = timestamps[sess_mask]
    prices = prices[sess_mask]
    sizes = sizes[sess_mask]

    if len(timestamps) < SUSTAIN_BARS:
        return {**base, "no_entry_reason": "insufficient_session_trades"}

    # ── Prior-close (Rule B) ────────────────────────────────────────────
    prev_close = get_prev_close(spec.ticker, spec.date)
    if prev_close is None or not math.isfinite(prev_close) or prev_close <= 0.0:
        return {**base, "no_entry_reason": "no_prev_close"}

    # ── Full 1-minute bar array (for VWAP and forward metrics) ──────────
    opens, highs, lows, closes, volumes, dvols, bar_starts = _build_1min_bars(
        timestamps, prices, sizes, session_start_ns, session_end_ns,
    )
    n_bars = len(bar_starts)
    if n_bars == 0:
        return {**base, "prev_close": float(prev_close), "no_entry_reason": "no_bars"}

    vwap = _compute_vwap_per_bar(bar_starts, dvols, volumes, session_start_ns)

    # ── Gate A: prevClose calibration check (Rule F / hindsight guard) ──
    # Tier 1, event day: peak intraday % vs catalog {MOM} must be loosely consistent.
    # Tier 0, first cross: recorded pct_change vs our prevClose reconstruction.
    if spec.tier == "tier1" and spec.is_event_day and not math.isnan(spec.mom_pct or float("nan")):
        peak_pct = (float(np.max(prices)) - prev_close) / prev_close * 100.0
        rel_err = abs(peak_pct - spec.mom_pct) / max(abs(spec.mom_pct), 1.0)
        if rel_err > GATE_A_REL_TOL_TIER1:
            log.warning(
                "GATE A Tier1 %s %s: peak_intraday=%.1f%% catalog_mom=%.1f%% "
                "rel_err=%.0f%% > %.0f%% — prevClose source mismatch",
                spec.ticker, spec.date, peak_pct, spec.mom_pct,
                rel_err * 100, GATE_A_REL_TOL_TIER1 * 100,
            )
            return {
                **base,
                "prev_close": float(prev_close),
                "no_entry_reason": "gate_a_mismatch",
                "peak_intraday_pct": round(peak_pct, 3),
                "catalog_mom_pct": spec.mom_pct,
            }

    # ── Build poll sequence (Rule A / C) ────────────────────────────────
    poll_interval_ns = int(poll_interval_s * NS_PER_SECOND)

    if spec.recorded_polls is not None:
        # Tier 0: use recorded scanner poll timestamps + Polygon pct_change fraction.
        # All entries already have pct_change >= 0.30 (scanner pre-filter).
        # First recorded poll = candidate.
        poll_items = spec.recorded_polls  # [(ts_ns, pct_frac), ...]
    else:
        # Tier 1: build synthetic poll grid, compute pct from prevClose.
        grid_ns = _build_poll_grid(
            session_start_ns,
            int(timestamps[0]),
            int(timestamps[-1]),
            poll_interval_ns,
        )
        poll_items = []
        for poll_ts in grid_ns:
            p = _price_at(poll_ts, timestamps, prices)
            if not math.isnan(p):
                poll_items.append((poll_ts, (p - prev_close) / prev_close))

    # ── Find candidate poll: first 30% cross (Rule C, one-shot) ─────────
    candidate_ts_ns: Optional[int] = None
    candidate_pct_frac: float = float("nan")

    for poll_ts_ns, pct_frac in poll_items:
        if pct_frac >= 0.30:
            candidate_ts_ns = int(poll_ts_ns)
            candidate_pct_frac = float(pct_frac)
            break

    if candidate_ts_ns is None:
        return {**base, "prev_close": float(prev_close), "no_entry_reason": "no_30pct_cross"}

    # Actual trade price at the candidate poll moment
    cross_price = _price_at(candidate_ts_ns, timestamps, prices)

    # Gate A Tier 0: check prevClose consistency against recorded Polygon value.
    if spec.tier == "tier0" and not math.isnan(cross_price) and prev_close > 0:
        reconstructed_frac = (cross_price - prev_close) / prev_close
        if not math.isnan(candidate_pct_frac) and abs(candidate_pct_frac) > 0.01:
            rel_err = abs(reconstructed_frac - candidate_pct_frac) / abs(candidate_pct_frac)
            if rel_err > GATE_A_REL_TOL_TIER0:
                log.warning(
                    "GATE A Tier0 calibration %s %s: "
                    "recorded_pct=%.3f reconstructed_pct=%.3f rel_err=%.1f%% "
                    "— prevClose source may differ from Polygon",
                    spec.ticker, spec.date,
                    candidate_pct_frac, reconstructed_frac, rel_err * 100,
                )

    # ── Candidate bar index ──────────────────────────────────────────────
    candidate_bar_idx = _find_bar_idx(bar_starts, candidate_ts_ns)
    if candidate_bar_idx < 0:
        candidate_bar_idx = 0  # before first bar; treat as bar 0

    # ── Setup filter on truncated ticks up to candidate_ts_ns (Rule D) ──
    # Running SF on truncated data is causal: only data available up to
    # the candidate poll is used.  first_qualify_bar is an index into
    # the truncated bar array, which is a prefix of the full bar array
    # (same indices for shared bars).
    mask_sf = timestamps <= candidate_ts_ns
    if not np.any(mask_sf):
        return {
            **base, "prev_close": float(prev_close),
            "no_entry_reason": "no_ticks_at_candidate",
        }

    sf = run_setup_filter(
        timestamps=timestamps[mask_sf],
        prices=prices[mask_sf],
        sizes=sizes[mask_sf],
        session_start_ns=session_start_ns,
        session_end_ns=session_end_ns,
    )

    if not sf.passes or sf.first_qualify_bar < 0:
        return {
            **base,
            "prev_close": float(prev_close),
            "no_entry_reason": "sf_not_qualifying",
            "candidate_bar": candidate_bar_idx,
            "first_qualify_bar": sf.first_qualify_bar,
            "candidate_ts_ns": candidate_ts_ns,
            "cross_pct": round(candidate_pct_frac * 100.0, 2),
            "cross_price": float(cross_price),
            "sf_mean_q": round(float(sf.mean_q_tilde), 4),
            "sf_weakest": sf.weakest_signal,
        }

    # ── Armed bar (Rule D, THE LOOK-AHEAD TRAP fix) ─────────────────────
    # first_qualify_bar is the START index of the 15-bar sustained window.
    # first_qualify_bar + SUSTAIN_BARS - 1 is the END index (last bar of window).
    # We cannot act until that last bar has closed — arming at first_qualify_bar
    # would mean we decided before observing all 15 bars, which is look-ahead.
    # When SF is run causally (truncated to candidate_ts_ns), the window must
    # already be complete within the truncated data, so armed_bar == candidate_bar_idx
    # in the common case.  The max() protects edge cases.
    armed_bar_idx = max(candidate_bar_idx, sf.first_qualify_bar + SUSTAIN_BARS - 1)

    # Gate B assertions (fail loudly if violated)
    _sustain_end = sf.first_qualify_bar + SUSTAIN_BARS - 1
    assert armed_bar_idx >= _sustain_end, (
        f"GATE B VIOLATION {spec.ticker} {spec.date}: "
        f"armed={armed_bar_idx} < first_qualify+{SUSTAIN_BARS-1}={_sustain_end}"
    )

    if armed_bar_idx >= n_bars:
        return {
            **base,
            "prev_close": float(prev_close),
            "no_entry_reason": "armed_beyond_session",
            "candidate_bar": candidate_bar_idx,
            "first_qualify_bar": sf.first_qualify_bar,
            "armed_bar": armed_bar_idx,
            "n_bars": n_bars,
        }

    # ── VWAP entry: first bar close > VWAP at or after armed_bar (Rule E) ─
    entry_bar_idx: Optional[int] = None
    for b in range(armed_bar_idx, n_bars):
        if vwap[b] > 0.0 and closes[b] > vwap[b]:
            entry_bar_idx = b
            break

    if entry_bar_idx is None:
        return {
            **base,
            "prev_close": float(prev_close),
            "no_entry_reason": "no_vwap_cross",
            "candidate_bar": candidate_bar_idx,
            "first_qualify_bar": sf.first_qualify_bar,
            "armed_bar": armed_bar_idx,
            "candidate_ts_ns": candidate_ts_ns,
            "cross_pct": round(candidate_pct_frac * 100.0, 2),
            "cross_price": float(cross_price),
            "sf_mean_q": round(float(sf.mean_q_tilde), 4),
            "sf_weakest": sf.weakest_signal,
        }

    # Gate B assertion: entry >= armed
    assert entry_bar_idx >= armed_bar_idx, (
        f"GATE B VIOLATION {spec.ticker} {spec.date}: "
        f"entry_bar={entry_bar_idx} < armed_bar={armed_bar_idx}"
    )

    # ── Entry price and metadata ─────────────────────────────────────────
    entry_bar_start_ns = int(bar_starts[entry_bar_idx])
    entry_ts_ns = entry_bar_start_ns + 60 * NS_PER_SECOND   # bar close
    entry_price = float(closes[entry_bar_idx])
    entry_vwap = float(vwap[entry_bar_idx])
    offset_ns = entry_ts_ns - session_start_ns
    bucket = _session_bucket(offset_ns)
    entry_t_sec = float(offset_ns) / NS_PER_SECOND

    # First tick after entry bar (for slippage proxy only — not a decision input)
    next_mask = timestamps >= entry_ts_ns
    first_tick_after = float(prices[next_mask][0]) if np.any(next_mask) else entry_price
    slippage_pct = (first_tick_after - entry_price) / entry_price

    # ── Forward metrics (evaluation only, post-entry data) ──────────────
    fwd = _compute_forward_metrics(
        entry_ts_ns=entry_ts_ns,
        entry_price=entry_price,
        timestamps=timestamps,
        prices=prices,
        session_end_ns=session_end_ns,
    )

    return {
        **base,
        "prev_close": float(prev_close),
        "no_entry_reason": None,
        # Candidate (30% cross poll)
        "candidate_ts_ns": int(candidate_ts_ns),
        "candidate_bar": int(candidate_bar_idx),
        "cross_pct": round(candidate_pct_frac * 100.0, 2),
        "cross_price": float(cross_price),
        # Setup filter
        "first_qualify_bar": int(sf.first_qualify_bar),
        "sf_mean_q": round(float(sf.mean_q_tilde), 4),
        "sf_weakest": sf.weakest_signal,
        # Armed bar
        "armed_bar": int(armed_bar_idx),
        # Entry
        "entry_ts_ns": int(entry_ts_ns),
        "entry_bar": int(entry_bar_idx),
        "entry_price": float(entry_price),
        "entry_vwap": float(entry_vwap),
        "entry_t_sec": round(entry_t_sec, 1),
        "session_bucket": bucket,
        "slippage_pct": round(slippage_pct, 5),
        # Forward metrics (Rule F: MOM% not used here)
        **fwd,
    }
