"""
Phase SEB-X Task 1 -- Build and cache forward paths for frozen SEB entries.

Loads entries.parquet (990 Tier-1 entries), replays catalog ticks forward
from each entry timestamp, computes per-event sigma (ATR over armed window),
and stores per-bar path arrays.

Hard constraints (from Phase SEB-X spec):
  - No entry recomputation. Read entries.parquet; that is the input boundary.
  - No live tables, no schema changes.
  - sigma window = armed_bar to entry_bar (capped at 14 bars, variable).
  - Fallback sigma = cross-event median for sparse windows (<3 bars or sigma=0).

Gate A: recomputed MFE(5/15/30) bar-level medians must match SEB report
        (3.23 / 5.62 / 7.20%) within 15% relative tolerance.  STOP if failed.

Output: results/seb_x/paths.parquet  (wide form, list columns for path arrays)
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROJECT_ROOT = _REPO_ROOT.parent
for _p in (str(_PROJECT_ROOT), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from data.loaders.trades import _session_ns_bounds          # noqa: E402
from data.schemas.mom_db import NS_PER_SECOND               # noqa: E402
from setup_filter import _build_1min_bars                   # noqa: E402
from tools.seb.feed import load_ticks_for_session           # noqa: E402
from tools.seb.simulator import _compute_vwap_per_bar       # noqa: E402

log = logging.getLogger(__name__)

ENTRIES_PARQUET = _REPO_ROOT / "results" / "seb" / "entries.parquet"
OUTPUT_DIR      = _REPO_ROOT / "results" / "seb_x"
PATHS_PARQUET   = OUTPUT_DIR / "paths.parquet"

MAX_PATH_BARS    = 180   # 3 hours of 1-min bars
SIGMA_MIN_BARS   = 3     # below this: use cross-event median sigma
SIGMA_WINDOW_CAP = 14    # cap on armed-window bars for sigma

# Gate A: SEB report medians and tolerance
GATE_A_TOL      = 0.15
SEB_MFE_5M_MED  = 0.0323
SEB_MFE_15M_MED = 0.0562
SEB_MFE_30M_MED = 0.0720


def _compute_sigma(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    armed_bar: int,
    entry_bar: int,
) -> tuple[float, float, int]:
    """Return (sigma_primary, sigma_robust, window_bars).

    sigma_primary = mean ATR over armed window (capped at 14 bars).
    sigma_robust  = median ATR (handles spike at armed bar).
    window_bars   = number of bars in the window (before zero-volume removal).
    """
    n_total = entry_bar - armed_bar + 1  # always >= 1
    start = max(armed_bar, entry_bar - SIGMA_WINDOW_CAP + 1)

    trs: list[float] = []
    prev_c = float(closes[start - 1]) if start > 0 else None

    for i in range(start, entry_bar + 1):
        h, l = float(highs[i]), float(lows[i])
        hl = h - l
        if prev_c is not None:
            tr = max(hl, abs(h - prev_c), abs(l - prev_c))
        else:
            tr = hl
        prev_c = float(closes[i])
        if tr > 0:
            trs.append(tr)

    if not trs:
        return 0.0, 0.0, n_total

    arr = np.array(trs, dtype=np.float64)
    return float(np.mean(arr)), float(np.median(arr)), n_total


def build_paths() -> pd.DataFrame:
    """Load frozen SEB entries, replay ticks, build paths. Return summary DataFrame."""
    log.info("Loading entries from %s", ENTRIES_PARQUET)
    all_df = pd.read_parquet(str(ENTRIES_PARQUET))
    entries = all_df[all_df["no_entry_reason"].isna()].copy()
    n_entries = len(entries)
    log.info("  %d actual entries (no_entry_reason == null)", n_entries)

    for col in ("armed_bar", "entry_bar"):
        entries[col] = entries[col].astype(int)

    records: list[dict] = []
    n_skipped = 0

    for i, (_, row) in enumerate(entries.iterrows()):
        if i % 100 == 0:
            log.info("  building paths: %d / %d", i, n_entries)

        ticker      = str(row["ticker"])
        date        = str(row["date"])
        armed_bar   = int(row["armed_bar"])
        entry_bar   = int(row["entry_bar"])
        entry_price = float(row["entry_price"])
        entry_ts_ns = int(row["entry_ts_ns"])

        tick_data = load_ticks_for_session(ticker, date)
        if tick_data is None:
            log.warning("No ticks for %s %s -- skipping", ticker, date)
            n_skipped += 1
            continue

        timestamps, prices, sizes = tick_data
        session_start_ns, session_end_ns = _session_ns_bounds(date)

        sess_mask  = (timestamps >= session_start_ns) & (timestamps <= session_end_ns)
        timestamps = timestamps[sess_mask]
        prices     = prices[sess_mask]
        sizes      = sizes[sess_mask]

        if len(timestamps) == 0:
            log.warning("Empty session %s %s -- skipping", ticker, date)
            n_skipped += 1
            continue

        opens, highs, lows, closes, volumes, dvols, bar_starts = _build_1min_bars(
            timestamps, prices, sizes, session_start_ns, session_end_ns,
        )
        n_bars = len(bar_starts)
        vwap   = _compute_vwap_per_bar(bar_starts, dvols, volumes, session_start_ns)

        if entry_bar >= n_bars or armed_bar >= n_bars:
            log.warning(
                "entry_bar=%d or armed_bar=%d out of range (n_bars=%d) for %s %s -- skip",
                entry_bar, armed_bar, n_bars, ticker, date,
            )
            n_skipped += 1
            continue

        # -- Sigma --
        sigma_p, sigma_r, window_bars = _compute_sigma(highs, lows, closes, armed_bar, entry_bar)

        # -- Forward path --
        path_start = entry_bar + 1
        path_end   = min(n_bars, path_start + MAX_PATH_BARS)
        n_path     = max(0, path_end - path_start)

        if n_path > 0:
            ph = highs[path_start:path_end].astype(np.float64)
            pl = lows[path_start:path_end].astype(np.float64)
            pc = closes[path_start:path_end].astype(np.float64)
            pv = vwap[path_start:path_end].astype(np.float64)

            # Running MFE/MAE in fractional returns from entry
            cum_high = np.maximum.accumulate(ph)
            cum_low  = np.minimum.accumulate(pl)
            path_mfe_frac = (cum_high - entry_price) / entry_price
            path_mae_frac = (cum_low  - entry_price) / entry_price
        else:
            ph = pl = pc = pv = np.array([], dtype=np.float64)
            path_mfe_frac = path_mae_frac = np.array([], dtype=np.float64)

        # -- Recompute MFE/MAE at horizons from bar highs/lows (Gate A) --
        def _mfe_h(n_min: int) -> float:
            k = min(n_min, n_path)
            return float((np.max(ph[:k]) - entry_price) / entry_price) if k > 0 else 0.0

        def _mae_h(n_min: int) -> float:
            k = min(n_min, n_path)
            return float((np.min(pl[:k]) - entry_price) / entry_price) if k > 0 else 0.0

        mfe_5m_rc  = _mfe_h(5)
        mfe_15m_rc = _mfe_h(15)
        mfe_30m_rc = _mfe_h(30)
        mfe_45m_rc = _mfe_h(45)
        mae_5m_rc  = _mae_h(5)
        mae_15m_rc = _mae_h(15)
        mae_30m_rc = _mae_h(30)

        # -- Time-to-MFE and pre-MFE MAE (for diagnostics) --
        n_30 = min(30, n_path)
        if n_30 > 0 and mfe_30m_rc > 0:
            peak_idx            = int(np.argmax(ph[:n_30]))
            time_to_mfe_30m     = peak_idx + 1       # 1-based bar offset from entry
            pre_peak_low        = float(np.min(pl[:peak_idx + 1])) if peak_idx >= 0 else float(pl[0])
            mae_pre_mfe_30m     = (pre_peak_low - entry_price) / entry_price
        else:
            time_to_mfe_30m = -1
            mae_pre_mfe_30m = float("nan")

        is_runner_rc = mfe_30m_rc >= 0.05

        # -- Sigma divergence flag --
        sigma_diverge = (
            sigma_p > 0 and abs(sigma_p - sigma_r) / sigma_p > 0.5
        )

        records.append({
            "ticker":           ticker,
            "date":             date,
            "sigma":            sigma_p,
            "sigma_robust":     sigma_r,
            "sigma_source":     "window",   # overridden to 'fallback' below
            "sigma_window_bars": window_bars,
            "sigma_diverge":    sigma_diverge,
            "entry_price":      entry_price,
            "entry_ts_ns":      entry_ts_ns,
            "armed_bar":        armed_bar,
            "entry_bar":        entry_bar,
            "prev_close":       float(row.get("prev_close", float("nan"))),
            "session_bucket":   str(row["session_bucket"]),
            "mom_pct":          float(row["mom_pct"]) if not pd.isna(row["mom_pct"]) else float("nan"),
            "is_event_day":     bool(row["is_event_day"]),
            "is_runner_seb":    bool(row["is_runner"]) if row["is_runner"] is not None else False,
            "is_runner_rc":     is_runner_rc,
            # SEB values for Gate A
            "mfe_5m_seb":       float(row["mfe_5m"]),
            "mfe_15m_seb":      float(row["mfe_15m"]),
            "mfe_30m_seb":      float(row["mfe_30m"]),
            # Recomputed from bar paths
            "mfe_5m_rc":        mfe_5m_rc,
            "mfe_15m_rc":       mfe_15m_rc,
            "mfe_30m_rc":       mfe_30m_rc,
            "mfe_45m_rc":       mfe_45m_rc,
            "mae_5m_rc":        mae_5m_rc,
            "mae_15m_rc":       mae_15m_rc,
            "mae_30m_rc":       mae_30m_rc,
            "time_to_mfe_30m":  time_to_mfe_30m,
            "mae_pre_mfe_30m":  mae_pre_mfe_30m,
            "path_n_bars":      n_path,
            # Path arrays (list columns)
            "path_high":        ph.tolist(),
            "path_low":         pl.tolist(),
            "path_close":       pc.tolist(),
            "path_vwap":        pv.tolist(),
            "path_mfe_so_far":  path_mfe_frac.tolist(),
            "path_mae_so_far":  path_mae_frac.tolist(),
        })

    log.info("Built paths for %d entries (%d skipped)", len(records), n_skipped)

    # -- Sigma fallback pass --
    good_sigmas = [
        r["sigma"] for r in records
        if r["sigma_window_bars"] >= SIGMA_MIN_BARS and r["sigma"] > 0
    ]
    if good_sigmas:
        sigma_global_med = float(np.median(good_sigmas))
        sigma_floor      = float(np.percentile(good_sigmas, 10))
    else:
        sigma_global_med = 0.0
        sigma_floor      = 0.0

    log.info(
        "Sigma cross-event median=%.5f  floor(p10)=%.5f", sigma_global_med, sigma_floor,
    )

    n_fallback = 0
    for r in records:
        if (
            r["sigma_window_bars"] < SIGMA_MIN_BARS
            or r["sigma"] <= 0
            or r["sigma"] < sigma_floor
        ):
            r["sigma"]        = sigma_global_med
            r["sigma_source"] = "fallback"
            n_fallback       += 1

    return pd.DataFrame(records), sigma_global_med, sigma_floor, n_fallback


def gate_a_check(df: pd.DataFrame) -> bool:
    """Gate A: bar-level median MFE must reproduce SEB report within tolerance."""
    passed = True
    checks = [
        ("mfe_5m_rc",  SEB_MFE_5M_MED,  "MFE(5m)"),
        ("mfe_15m_rc", SEB_MFE_15M_MED, "MFE(15m)"),
        ("mfe_30m_rc", SEB_MFE_30M_MED, "MFE(30m)"),
    ]
    for col, ref, label in checks:
        med     = float(df[col].median())
        rel_err = abs(med - ref) / max(abs(ref), 1e-9)
        ok      = rel_err <= GATE_A_TOL
        log.info(
            "Gate A %s: ref=%.4f recomp=%.4f rel_err=%.1f%% [%s]",
            label, ref, med, rel_err * 100, "OK" if ok else "FAIL",
        )
        if not ok:
            passed = False
    return passed


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    df, sigma_med, sigma_floor, n_fallback = build_paths()

    if df.empty:
        log.error("No paths built -- check entries.parquet")
        sys.exit(1)

    ok = gate_a_check(df)
    if not ok:
        log.error("Gate A FAILED -- paths do not reproduce SEB MFE medians. STOP.")
        sys.exit(1)

    df.to_parquet(str(PATHS_PARQUET), index=False)
    log.info("Wrote %s (%d rows)", PATHS_PARQUET, len(df))

    print("")
    print("[SEB-X Task 1] Paths built: %d" % len(df))
    print("  sigma median:   %.5f" % sigma_med)
    print("  sigma floor(p10): %.5f" % sigma_floor)
    print("  fallback rate:  %d/%d (%.1f%%)" % (n_fallback, len(df), 100.0 * n_fallback / max(1, len(df))))
    print("  sigma_diverge:  %d entries (primary vs robust differ >50%%)" % int(df["sigma_diverge"].sum()))
    print("  MFE(30m) median (rc): %.2f%%" % (df["mfe_30m_rc"].median() * 100))
    print("  runner rate (rc):     %.1f%%" % (df["is_runner_rc"].mean() * 100))
    print("  Gate A: PASS")
    print("  Paths -> %s" % PATHS_PARQUET)


if __name__ == "__main__":
    main()
