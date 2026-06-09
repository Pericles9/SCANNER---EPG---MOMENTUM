"""
Phase CPD — Sub-phase CPD-0, Task T3 (b/c/d)
=============================================
Offline PELT segmentation of the WJI_log traces to establish ground-truth regime
boundaries and to derive the k / h prior ranges for the CPD-1 CUSUM sweep.

Method / scoping decisions (documented for the reader):
-------------------------------------------------------
- PELT runs on the **post-T_event** portion of WJI_log (active-sec-since-event ≥ 0).
  Rationale: the CUSUM gate only acts post-warmup, and the tradeable regime structure
  lives after ignition. The pre-event region is dominated by buy-side-kernel dead-tick
  flooring (log(WJI) heavy left tail, see T2) which would pollute segment statistics.
- The irregular tick series is resampled to a **uniform active-time grid** before PELT
  (PELT assumes uniform sampling and segments by index position). Bin width is adaptive:
  bin_w = max(2.0s, post_event_active_duration / TARGET_BINS) with TARGET_BINS=3000, so
  every event yields ≤ ~3000 points (bounds rbf cost) while spanning its full duration.
  Each bin holds the MEAN log(WJI) of the ticks in it; empty bins are forward-filled.
- Cost function "rbf" (handles mean AND variance shifts — WJI regimes shift both).
  Penalty = 2·ln(n_bins)  (BIC penalty for rbf, per plan T3b).
- Segment classification (CPD-0 analysis labels only, NOT a gate parameter):
  REST if mean log(WJI) < 0.5, REGIME if ≥ 0.5.
- Segment durations are reported in seconds AND in original trade-tick counts (the live
  CUSUM accumulates per trade tick, so the h range is derived from tick counts).

Inputs:  results/phase_cpd/cpd0/wji_traces.pkl  (T1)
Outputs:
  results/phase_cpd/cpd0/pelt_segments.json
  results/phase_cpd/cpd0/calibration_summary.json
  results/phase_cpd/cpd0/pelt_diagnostic_sample.html

Run:
  "D:/Trading Research/.venv/Scripts/python.exe" -m tools.phase_cpd.cpd0_t3_pelt
"""
from __future__ import annotations

import json
import math
import pickle
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import ruptures as rpt

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

OUT_DIR = REPO_ROOT / "results" / "phase_cpd" / "cpd0"
PKL_PATH = OUT_DIR / "wji_traces.pkl"

TARGET_BINS = 3000
MIN_BIN_W = 2.0
REGIME_MEAN_THRESHOLD = 0.5   # log(WJI) mean ≥ 0.5 → REGIME, else REST
MAX_WORKERS = 8


# ══════════════════════════════════════════════════════════════════════
#  Resampling + PELT (per event)
# ══════════════════════════════════════════════════════════════════════

def _resample_post_event(tse: np.ndarray, wji: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """
    Restrict to post-T_event ticks and resample log(WJI) onto a uniform active-time grid.

    Returns (grid_log, grid_centers_sec, bin_w) where grid_centers_sec[i] is the active
    seconds-since-event at the centre of bin i. Empty bins are forward-filled.
    """
    m = tse >= 0.0
    t = tse[m]
    w = wji[m]
    w = np.where(w > 0, w, 1e-9)
    lw = np.log(w)
    if len(t) < 4:
        return np.array([]), np.array([]), MIN_BIN_W

    dur = float(t[-1] - t[0])
    bin_w = max(MIN_BIN_W, dur / TARGET_BINS)
    n_bins = int(math.ceil(dur / bin_w)) + 1
    idx = np.clip(((t - t[0]) / bin_w).astype(int), 0, n_bins - 1)

    grid = np.full(n_bins, np.nan, dtype=np.float64)
    # mean per bin
    sums = np.zeros(n_bins); counts = np.zeros(n_bins)
    np.add.at(sums, idx, lw)
    np.add.at(counts, idx, 1.0)
    nz = counts > 0
    grid[nz] = sums[nz] / counts[nz]
    # forward-fill empties; back-fill any leading NaN
    last = grid[0] if not np.isnan(grid[0]) else 0.0
    for i in range(n_bins):
        if np.isnan(grid[i]):
            grid[i] = last
        else:
            last = grid[i]
    centers = t[0] + (np.arange(n_bins) + 0.5) * bin_w
    return grid, centers, bin_w


def pelt_worker(args: dict) -> dict:
    """Run PELT on one event's post-event WJI_log grid; return segment records."""
    key = args["key"]
    tse = args["tse"]
    wji = args["wji"]
    t_event_active = args["t_event_active"]

    grid, centers, bin_w = _resample_post_event(tse, wji)
    if len(grid) < 4:
        return {"key": key, "status": "skipped", "reason": "too_few_post_event_ticks"}

    n = len(grid)
    pen = 2.0 * math.log(n)
    algo = rpt.Pelt(model="rbf", min_size=2).fit(grid.reshape(-1, 1))
    bkps = algo.predict(pen=pen)          # list of segment END indices, ends with n
    starts = [0] + bkps[:-1]

    # original post-event tick times (for trade-tick durations)
    post_t = tse[tse >= 0.0]
    t0 = float(post_t[0]) if len(post_t) else 0.0

    segments = []
    changepoints = []
    for a, b in zip(starts, bkps):
        seg = grid[a:b]
        start_sec = t0 + a * bin_w
        end_sec = t0 + b * bin_w
        mean_log = float(seg.mean())
        n_trades = int(((post_t >= start_sec) & (post_t < end_sec)).sum())
        segments.append({
            "start_sec": round(start_sec, 1),
            "end_sec": round(end_sec, 1),
            "duration_sec": round((b - a) * bin_w, 1),
            "n_trades": n_trades,
            "mean_log": mean_log,
            "std_log": float(seg.std(ddof=1)) if len(seg) > 1 else 0.0,
            "label": "REGIME" if mean_log >= REGIME_MEAN_THRESHOLD else "REST",
        })
        if b < n:
            changepoints.append(round(end_sec, 1))

    return {
        "key": key, "status": "ok",
        "bin_w": round(bin_w, 3), "n_bins": n,
        "n_segments": len(segments),
        "changepoints_sec": changepoints,
        "segments": segments,
    }


# ══════════════════════════════════════════════════════════════════════
#  Aggregation → calibration summary
# ══════════════════════════════════════════════════════════════════════

def _pcts(a, ps):
    a = np.asarray(a, dtype=np.float64)
    if len(a) == 0:
        return {f"p{p}": None for p in ps}
    return {f"p{p}": round(float(np.percentile(a, p)), 3) for p in ps}


def build_calibration_summary(results: list[dict], sigma_log_median: float) -> dict:
    """Aggregate PELT output into k/h prior ranges and segment statistics."""
    rest_dur, regime_dur = [], []
    regime_ticks = []
    regime_mean_log = []
    rest_to_regime_elev = []   # REGIME-side mean_log at a REST→REGIME boundary
    regime_to_rest_elev = []   # REGIME-side mean_log at a REGIME→REST boundary
    segs_per_event = []

    for r in results:
        if r["status"] != "ok":
            continue
        segs = r["segments"]
        segs_per_event.append(len(segs))
        for i, s in enumerate(segs):
            if s["label"] == "REST":
                rest_dur.append(s["duration_sec"])
            else:
                regime_dur.append(s["duration_sec"])
                regime_ticks.append(s["n_trades"])
                regime_mean_log.append(s["mean_log"])
            # boundary transitions
            if i > 0:
                prev, cur = segs[i - 1]["label"], s["label"]
                if prev == "REST" and cur == "REGIME":
                    rest_to_regime_elev.append(s["mean_log"])
                elif prev == "REGIME" and cur == "REST":
                    regime_to_rest_elev.append(segs[i - 1]["mean_log"])

    # ── Recommended k range (plan T3c) ──
    # "transition elevation" = REGIME-side mean log(WJI) at REST→REGIME boundaries,
    # in RAW log units. k range = [p25·0.5, p75·1.5] rounded to 1 decimal.
    # CAVEAT (documented): k in the CUSUM accumulator is in STANDARDISED units
    # (deviation = log(WJI)/sigma_log). We therefore ALSO report the standardised
    # elevation (raw / sigma_log_median) so the T6 grid can be sanity-checked against
    # the proposal default k∈{0.5,1.0,1.5,2.0}.
    if rest_to_regime_elev:
        p25 = float(np.percentile(rest_to_regime_elev, 25))
        p75 = float(np.percentile(rest_to_regime_elev, 75))
    else:
        p25, p75 = float(np.percentile(regime_mean_log, 25)), float(np.percentile(regime_mean_log, 75))
    k_lo = round(p25 * 0.5, 1)
    k_hi = round(p75 * 1.5, 1)

    # ── Recommended h range (plan T3c) ──
    # [2, ceil(p75 of REGIME segment duration in TICKS / 10)] capped at 12.
    if regime_ticks:
        p75_ticks = float(np.percentile(regime_ticks, 75))
    else:
        p75_ticks = 20.0
    h_hi = min(12, int(math.ceil(p75_ticks / 10.0)))
    h_hi = max(h_hi, 4)  # ensure span ≥ 3 values between 2 and h_hi

    return {
        "method": "PELT rbf, pen=2·ln(n_bins), post-event uniform active-time grid "
                  f"(target {TARGET_BINS} bins, min bin {MIN_BIN_W}s). "
                  "REST/REGIME label threshold mean log(WJI)=0.5.",
        "n_events": sum(1 for r in results if r["status"] == "ok"),
        "segments_per_event": {
            "mean": round(float(np.mean(segs_per_event)), 2),
            **_pcts(segs_per_event, [10, 50, 90]),
        },
        "rest_segment_duration_sec": {
            "mean": round(float(np.mean(rest_dur)), 1) if rest_dur else None,
            "median": round(float(np.median(rest_dur)), 1) if rest_dur else None,
            **_pcts(rest_dur, [10, 90]),
            "n": len(rest_dur),
        },
        "regime_segment_duration_sec": {
            "mean": round(float(np.mean(regime_dur)), 1) if regime_dur else None,
            "median": round(float(np.median(regime_dur)), 1) if regime_dur else None,
            **_pcts(regime_dur, [10, 90]),
            "n": len(regime_dur),
        },
        "regime_segment_duration_ticks": {
            "median": round(float(np.median(regime_ticks)), 1) if regime_ticks else None,
            **_pcts(regime_ticks, [25, 75]),
        },
        "regime_log_elevation": {
            "mean": round(float(np.mean(regime_mean_log)), 3) if regime_mean_log else None,
            **_pcts(regime_mean_log, [25, 75]),
        },
        "rest_to_regime_transition_elevation_log": {
            **_pcts(rest_to_regime_elev, [25, 50, 75]), "n": len(rest_to_regime_elev),
        },
        "regime_to_rest_transition_elevation_log": {
            **_pcts(regime_to_rest_elev, [25, 50, 75]), "n": len(regime_to_rest_elev),
        },
        "sigma_log_median_from_T2": round(sigma_log_median, 4),
        "recommended_k_range_raw_log": [k_lo, k_hi],
        "recommended_k_range_standardised": [
            round(k_lo / max(sigma_log_median, 1e-6), 1),
            round(k_hi / max(sigma_log_median, 1e-6), 1),
        ],
        "recommended_h_range": [2, h_hi],
        "k_units_caveat": "k in the CUSUM accumulator is standardised (deviation = "
                          "log(WJI)/sigma_log). The raw-log k range above follows the plan "
                          "T3c formula literally; the standardised range is the apples-to-"
                          "apples comparison to the proposal default k∈{0.5,1.0,1.5,2.0}. "
                          "Reconcile when building the T6 sweep grid.",
    }


# ══════════════════════════════════════════════════════════════════════
#  Diagnostic chart (6 events)
# ══════════════════════════════════════════════════════════════════════

def _diagnostic_chart(traces, results_by_key, rep_keys, path):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=6, cols=2, shared_xaxes=False,
        subplot_titles=[t for k in rep_keys for t in
                        (f"{k} — log(WJI) + PELT segments", f"{k} — raw WJI")],
        vertical_spacing=0.05, horizontal_spacing=0.07,
    )
    for ri, key in enumerate(rep_keys):
        tr = traces[key]
        res = results_by_key.get(key)
        tse = tr["t_since_event_active"]
        wji = tr["wji"]
        m = tse >= 0.0
        t = tse[m]
        lw = np.log(np.where(wji[m] > 0, wji[m], 1e-9))
        row = ri + 1
        # Panel 1: log(WJI) + changepoints + segment shading
        fig.add_trace(go.Scatter(x=t, y=lw, mode="lines", line=dict(width=0.7, color="#4C78A8"),
                                 showlegend=False), row=row, col=1)
        if res and res["status"] == "ok":
            for s in res["segments"]:
                fig.add_vrect(x0=s["start_sec"], x1=s["end_sec"],
                              fillcolor=("#54A24B" if s["label"] == "REGIME" else "#BAB0AC"),
                              opacity=0.18, line_width=0, row=row, col=1)
            for cp in res["changepoints_sec"]:
                fig.add_vline(x=cp, line=dict(color="#E45756", width=1, dash="dash"),
                              row=row, col=1)
        # Panel 2: raw WJI
        fig.add_trace(go.Scatter(x=t, y=wji[m], mode="lines", line=dict(width=0.7, color="#72B7B2"),
                                 showlegend=False), row=row, col=2)
        fig.update_xaxes(title_text="active sec since T_event", row=row, col=1)
        fig.update_xaxes(title_text="active sec since T_event", row=row, col=2)

    fig.update_layout(height=1700, width=1300, template="plotly_white",
                      title="CPD-0 T3d — PELT diagnostic (3 highest / 3 lowest n_ticks). "
                            "Green=REGIME, grey=REST, red dashed=changepoint.")
    fig.write_html(str(path), include_plotlyjs="cdn")


# ══════════════════════════════════════════════════════════════════════
#  Driver
# ══════════════════════════════════════════════════════════════════════

def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    traces = pickle.load(open(PKL_PATH, "rb"))
    sigma_summary = json.load(open(OUT_DIR / "sigma_log_summary.json"))
    sigma_log_median = sigma_summary["sigma_log_median"]

    work = [{"key": k, "tse": t["t_since_event_active"], "wji": t["wji"],
             "t_event_active": t["t_event_active_sec"]} for k, t in traces.items()]

    results = []
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(pelt_worker, a) for a in work]
        for fut in as_completed(futs):
            results.append(fut.result())

    ok = [r for r in results if r["status"] == "ok"]
    print(f"PELT: {len(ok)}/{len(results)} events segmented")

    segments_out = {r["key"]: {k: r[k] for k in
                    ("bin_w", "n_bins", "n_segments", "changepoints_sec", "segments")}
                    for r in ok}
    with open(OUT_DIR / "pelt_segments.json", "w") as f:
        json.dump(segments_out, f, indent=2)

    calib = build_calibration_summary(results, sigma_log_median)
    with open(OUT_DIR / "calibration_summary.json", "w") as f:
        json.dump(calib, f, indent=2)

    # Diagnostic chart: 3 highest / 3 lowest n_ticks
    by_ticks = sorted(traces.items(), key=lambda kv: kv[1]["n_ticks"])
    rep = [k for k, _ in by_ticks[-3:]] + [k for k, _ in by_ticks[:3]]
    results_by_key = {r["key"]: r for r in results}
    _diagnostic_chart(traces, results_by_key, rep, OUT_DIR / "pelt_diagnostic_sample.html")

    print("\n── Calibration summary ──")
    print(f"  segments/event: mean={calib['segments_per_event']['mean']} "
          f"p10/50/90={calib['segments_per_event']['p10']}/{calib['segments_per_event']['p50']}/{calib['segments_per_event']['p90']}")
    print(f"  REST  dur sec: median={calib['rest_segment_duration_sec']['median']} (n={calib['rest_segment_duration_sec']['n']})")
    print(f"  REGIME dur sec: median={calib['regime_segment_duration_sec']['median']} (n={calib['regime_segment_duration_sec']['n']})")
    print(f"  REGIME dur ticks p25/75: {calib['regime_segment_duration_ticks']['p25']}/{calib['regime_segment_duration_ticks']['p75']}")
    print(f"  REST→REGIME elev (log) p25/50/75: {calib['rest_to_regime_transition_elevation_log']['p25']}/"
          f"{calib['rest_to_regime_transition_elevation_log']['p50']}/{calib['rest_to_regime_transition_elevation_log']['p75']}"
          f"  (n={calib['rest_to_regime_transition_elevation_log']['n']})")
    print(f"  recommended k range (raw log)      : {calib['recommended_k_range_raw_log']}")
    print(f"  recommended k range (standardised) : {calib['recommended_k_range_standardised']}")
    print(f"  recommended h range                : {calib['recommended_h_range']}")
    print(f"\nWritten: pelt_segments.json, calibration_summary.json, pelt_diagnostic_sample.html")


if __name__ == "__main__":
    main()
