"""
Phase SEB-X v2 Task 1 -- Compute v2 sigma context for 990 frozen SEB entries.

sigma v2 definition (all in price units at entry):
  sigma_primary = entry_price * sqrt( mean(ln(H/L)^2) / (4*ln2) )
                  over trailing LOOKBACK_N=20 bars before entry_bar (Parkinson).
  sigma_robust  = entry_price * sqrt( median(ln(H/L)^2) / (4*ln2) )
  sigma_vwap    = stdev(bar_close - session_vwap) over same look-back (already $)
  ADR           = avg RTH high-low range over T-3..T-1 prior sessions
  sigma_floor   = ADR_C * ADR   (ADR_C=0.05, FLAGGED KNOB)
  sigma_final   = max(sigma_primary, sigma_floor)
  sigma_source  = "intraday" | "floor_bound"

Reuses v1 paths.parquet for MFE values and forward paths (do NOT rebuild them).

Gate A:
  (a) MFE(5/15/30) medians from paths.parquet must match v1 [3.23/5.62/7.20%] +/-15%.
  (b) sigma must be non-degenerate: <50% floor-bound AND CV(sigma_final) >= 0.10.
  STOP on any failure -- do not proceed to sweep.

Output: results/seb_x_v2/sigma_context.parquet
"""
from __future__ import annotations

import datetime
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT    = Path(__file__).resolve().parents[2]
_PROJECT_ROOT = _REPO_ROOT.parent
for _p in (str(_PROJECT_ROOT), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from data.loaders.trades import _session_ns_bounds          # noqa: E402
from data.schemas.mom_db import NS_PER_SECOND               # noqa: E402
from setup_filter import _build_1min_bars                   # noqa: E402
from tools.seb.feed import _AVAILABLE_DIRS, load_ticks_for_session  # noqa: E402
from tools.seb.simulator import _compute_vwap_per_bar       # noqa: E402

log = logging.getLogger(__name__)

ENTRIES_PARQUET  = _REPO_ROOT / "results" / "seb" / "entries.parquet"
PATHS_V1_PARQUET = _REPO_ROOT / "results" / "seb_x" / "paths.parquet"
OUTPUT_DIR       = _REPO_ROOT / "results" / "seb_x_v2"
SIGMA_PARQUET    = OUTPUT_DIR / "sigma_context.parquet"

LOOKBACK_N  = 20     # bars before entry_bar for Parkinson + sigma_vwap
ADR_DAYS    = 3      # prior sessions for ADR
ADR_C       = 0.05   # FLAGGED KNOB: sigma_floor = ADR_C * ADR

# Gate A: reference MFE medians from v1 SEB report
GATE_A_TOL       = 0.15
SEB_MFE_5M_MED   = 0.0323
SEB_MFE_15M_MED  = 0.0562
SEB_MFE_30M_MED  = 0.0720

# Gate A sigma: stop thresholds
GATE_A_FLOOR_BOUND_MAX = 0.50   # >50% floor-bound -> degenerate
GATE_A_CV_MIN          = 0.10   # CV < 0.10 -> effectively constant


def _parkinson_frac(highs: np.ndarray, lows: np.ndarray) -> tuple[float, float]:
    """Parkinson high-low range estimator, returns (primary_frac, robust_frac).

    Both are log-return-scale sigma per bar. Multiply by entry_price for price units.
    primary = sqrt(mean(ln(H/L)^2) / 4ln2)
    robust  = sqrt(median(ln(H/L)^2) / 4ln2)
    """
    mask = (highs > lows) & (highs > 0) & (lows > 0)
    if mask.sum() < 2:
        return 0.0, 0.0
    h, l   = highs[mask].astype(np.float64), lows[mask].astype(np.float64)
    lr2    = np.log(h / l) ** 2
    factor = 4.0 * np.log(2.0)
    return float(np.sqrt(np.mean(lr2)   / factor)), \
           float(np.sqrt(np.median(lr2) / factor))


def _vwap_dev_sigma(closes: np.ndarray, vwap: np.ndarray) -> float:
    """Stdev of (close - vwap) over look-back window, in price units."""
    if len(closes) < 2:
        return 0.0
    devs = closes.astype(np.float64) - vwap.astype(np.float64)
    return float(np.std(devs, ddof=1))


def _get_adr(ticker: str, entry_date: str) -> tuple[float, int]:
    """Average RTH high-low range over up to ADR_DAYS prior sessions.

    Returns (adr_price, n_sessions_used).
    RTH = 09:30-16:00 ET (5.5h and 12h offsets from session_start_ns).
    """
    dt = datetime.date.fromisoformat(entry_date)
    ranges: list[float] = []
    delta = 1
    while len(ranges) < ADR_DAYS and delta <= 20:
        prev_date = (dt - datetime.timedelta(days=delta)).isoformat()
        delta    += 1
        if (ticker, prev_date) not in _AVAILABLE_DIRS:
            continue
        tick_data = load_ticks_for_session(ticker, prev_date)
        if tick_data is None:
            continue
        ts, prices, _ = tick_data
        sess_start, _ = _session_ns_bounds(prev_date)
        rth_start = sess_start + int(5.5 * 3600 * NS_PER_SECOND)
        rth_end   = sess_start + int(12.0 * 3600 * NS_PER_SECOND)
        mask   = (ts >= rth_start) & (ts <= rth_end)
        p_rth  = prices[mask]
        if len(p_rth) < 10:
            continue
        ranges.append(float(np.max(p_rth) - np.min(p_rth)))

    if not ranges:
        return 0.0, 0
    return float(np.mean(ranges)), len(ranges)


def build_sigma_context() -> pd.DataFrame:
    """Compute v2 sigma for all 990 frozen SEB entries. Returns DataFrame."""
    log.info("Loading entries from %s", ENTRIES_PARQUET)
    all_df  = pd.read_parquet(str(ENTRIES_PARQUET))
    entries = all_df[all_df["no_entry_reason"].isna()].copy()
    n_entries = len(entries)
    log.info("  %d actual entries", n_entries)

    for col in ("armed_bar", "entry_bar"):
        entries[col] = entries[col].astype(int)

    records: list[dict] = []
    n_skipped = 0

    for i, (_, row) in enumerate(entries.iterrows()):
        if i % 100 == 0:
            log.info("  processing: %d / %d", i, n_entries)

        ticker      = str(row["ticker"])
        date        = str(row["date"])
        entry_bar   = int(row["entry_bar"])
        entry_price = float(row["entry_price"])

        tick_data = load_ticks_for_session(ticker, date)
        if tick_data is None:
            log.warning("No ticks for %s %s -- skip", ticker, date)
            n_skipped += 1
            continue

        timestamps, prices, sizes = tick_data
        session_start_ns, session_end_ns = _session_ns_bounds(date)

        sess_mask  = (timestamps >= session_start_ns) & (timestamps <= session_end_ns)
        timestamps = timestamps[sess_mask]
        prices     = prices[sess_mask]
        sizes      = sizes[sess_mask]

        if len(timestamps) == 0:
            log.warning("Empty session %s %s -- skip", ticker, date)
            n_skipped += 1
            continue

        opens, highs, lows, closes, volumes, dvols, bar_starts = _build_1min_bars(
            timestamps, prices, sizes, session_start_ns, session_end_ns,
        )
        n_bars = len(bar_starts)
        vwap   = _compute_vwap_per_bar(bar_starts, dvols, volumes, session_start_ns)

        if entry_bar >= n_bars:
            log.warning("entry_bar=%d out of range (n_bars=%d) for %s %s -- skip",
                        entry_bar, n_bars, ticker, date)
            n_skipped += 1
            continue

        # Look-back window: LOOKBACK_N bars strictly before entry_bar
        lb_start = max(0, entry_bar - LOOKBACK_N)
        lb_end   = entry_bar   # exclusive
        n_lb     = lb_end - lb_start

        if n_lb >= 2:
            lb_h = highs[lb_start:lb_end]
            lb_l = lows[lb_start:lb_end]
            lb_c = closes[lb_start:lb_end]
            lb_v = vwap[lb_start:lb_end]

            p_frac, p_rob_frac = _parkinson_frac(lb_h, lb_l)
            sigma_primary = p_frac     * entry_price
            sigma_robust  = p_rob_frac * entry_price
            sigma_vwap    = _vwap_dev_sigma(lb_c, lb_v)
        else:
            sigma_primary = sigma_robust = sigma_vwap = 0.0
            n_lb = 0

        # ADR floor from prior sessions
        adr, adr_n_sessions = _get_adr(ticker, date)
        sigma_floor = ADR_C * adr
        sigma_final = max(sigma_primary, sigma_floor) if sigma_primary > 0 else sigma_floor
        sigma_source = "intraday" if sigma_primary >= sigma_floor else "floor_bound"

        records.append({
            "ticker":            ticker,
            "date":              date,
            "sigma_primary":     sigma_primary,
            "sigma_robust":      sigma_robust,
            "sigma_vwap":        sigma_vwap,
            "adr":               adr,
            "sigma_floor":       sigma_floor,
            "sigma_final":       sigma_final,
            "sigma_source":      sigma_source,
            "lookback_n":        n_lb,
            "adr_n_sessions":    adr_n_sessions,
            "entry_price":       entry_price,
            "entry_bar":         entry_bar,
        })

    log.info("Computed sigma for %d entries (%d skipped)", len(records), n_skipped)
    return pd.DataFrame(records)


def gate_a_check_mfe(paths_parquet: Path) -> bool:
    """Gate A (a): MFE medians from paths.parquet must match v1 +/-15%."""
    if not paths_parquet.exists():
        log.error("paths.parquet not found at %s", paths_parquet)
        return False

    df = pd.read_parquet(str(paths_parquet))
    checks = [
        ("mfe_5m_rc",  SEB_MFE_5M_MED,  "MFE(5m)"),
        ("mfe_15m_rc", SEB_MFE_15M_MED, "MFE(15m)"),
        ("mfe_30m_rc", SEB_MFE_30M_MED, "MFE(30m)"),
    ]
    passed = True
    for col, ref, label in checks:
        if col not in df.columns:
            log.error("Gate A: column %s missing from paths.parquet", col)
            return False
        med     = float(df[col].median())
        rel_err = abs(med - ref) / max(abs(ref), 1e-9)
        ok      = rel_err <= GATE_A_TOL
        log.info("Gate A %s: ref=%.4f recomp=%.4f rel_err=%.1f%% [%s]",
                 label, ref, med, rel_err * 100, "OK" if ok else "FAIL")
        if not ok:
            passed = False
    return passed


def gate_a_check_sigma(df: pd.DataFrame) -> bool:
    """Gate A (b): sigma_final must be non-degenerate.

    FAIL if >50% floor-bound or CV(sigma_final) < 0.10.
    """
    n = len(df)
    if n == 0:
        return False

    floor_bound_n    = int((df["sigma_source"] == "floor_bound").sum())
    floor_bound_rate = floor_bound_n / n
    sfinal           = df["sigma_final"].values.astype(np.float64)
    sfinal_mean      = float(np.mean(sfinal))
    sfinal_std       = float(np.std(sfinal))
    cv               = sfinal_std / sfinal_mean if sfinal_mean > 0 else 0.0

    log.info("Gate A sigma: n=%d  floor_bound=%d (%.1f%%)  CV=%.3f",
             n, floor_bound_n, floor_bound_rate * 100, cv)

    passed = True
    if floor_bound_rate > GATE_A_FLOOR_BOUND_MAX:
        log.error("Gate A sigma FAIL: %.1f%% floor-bound > 50%% threshold", floor_bound_rate * 100)
        passed = False
    if cv < GATE_A_CV_MIN:
        log.error("Gate A sigma FAIL: CV=%.3f < %.2f (sigma effectively constant)", cv, GATE_A_CV_MIN)
        passed = False
    if passed:
        log.info("Gate A sigma: PASS")
    return passed


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Gate A (a): MFE reuse integrity
    log.info("Gate A (a): checking MFE reuse integrity from v1 paths.parquet")
    if not gate_a_check_mfe(PATHS_V1_PARQUET):
        log.error("Gate A (a) FAILED -- v1 paths.parquet MFE mismatch. STOP.")
        sys.exit(1)
    log.info("Gate A (a): PASS (paths.parquet MFE medians unchanged)")

    # Build sigma context
    df = build_sigma_context()
    if df.empty:
        log.error("No sigma computed -- check entries.parquet and tick data.")
        sys.exit(1)

    # Gate A (b): sigma distribution
    if not gate_a_check_sigma(df):
        log.error("Gate A (b) FAILED -- sigma v2 degenerate. STOP. Do not proceed to sweep.")
        sys.exit(1)

    df.to_parquet(str(SIGMA_PARQUET), index=False)
    log.info("Wrote %s (%d rows)", SIGMA_PARQUET, len(df))

    # Summary
    sfinal   = df["sigma_final"].values
    svwap    = df["sigma_vwap"].values
    sprimary = df["sigma_primary"].values
    fb_n     = int((df["sigma_source"] == "floor_bound").sum())
    cv       = float(np.std(sfinal) / np.mean(sfinal)) if np.mean(sfinal) > 0 else 0.0

    print("")
    print("[SEB-X v2 Task 1] sigma context built: %d entries" % len(df))
    print("  lookback_n = %d bars" % LOOKBACK_N)
    print("  ADR_C = %.2f (floor knob)" % ADR_C)
    print("")
    print("  sigma_primary (Parkinson) distribution:")
    pcts = np.percentile(sprimary[sprimary > 0], [10, 25, 50, 75, 90])
    print("     p10    p25    p50    p75    p90")
    print("  %6.4f %6.4f %6.4f %6.4f %6.4f" % tuple(pcts))
    print("")
    print("  sigma_vwap distribution:")
    pcts_v = np.percentile(svwap[svwap > 0], [10, 25, 50, 75, 90])
    print("     p10    p25    p50    p75    p90")
    print("  %6.4f %6.4f %6.4f %6.4f %6.4f" % tuple(pcts_v))
    print("")
    print("  sigma_final distribution:")
    pcts_f = np.percentile(sfinal, [10, 25, 50, 75, 90])
    print("     p10    p25    p50    p75    p90")
    print("  %6.4f %6.4f %6.4f %6.4f %6.4f" % tuple(pcts_f))
    print("  CV(sigma_final) = %.3f" % cv)
    print("  floor_bound: %d / %d (%.1f%%)" % (fb_n, len(df), 100.0 * fb_n / max(1, len(df))))
    print("")
    print("  ADR stats:")
    adr_avail = df[df["adr"] > 0]["adr"].values
    if len(adr_avail) > 0:
        pcts_a = np.percentile(adr_avail, [10, 25, 50, 75, 90])
        print("     p10    p25    p50    p75    p90")
        print("  %6.4f %6.4f %6.4f %6.4f %6.4f" % tuple(pcts_a))
    zero_adr = int((df["adr"] == 0).sum())
    if zero_adr > 0:
        print("  NOTE: %d entries had no prior session ADR data (adr=0)" % zero_adr)
    print("")
    print("  Gate A (a) MFE reuse: PASS")
    print("  Gate A (b) sigma non-degenerate: PASS")
    print("  Output -> %s" % SIGMA_PARQUET)


if __name__ == "__main__":
    main()
