"""
Phase CPD — Sub-phase CPD-0, Task T2
=====================================
Log-ratio transform + symmetry verification + sigma_log estimation.

Background (Cooper Option A): WJI_background(t) ≡ 1.0, so
    WJI_log(t) = log( WJI(t) / WJI_background(t) ) = log( WJI(t) ).

H0 / rest-state sample window (per proposal §8 and plan T2 — the "warmup window",
i.e. the first 300s of ACTIVE time after T_event). NOTE / finding: for these momentum
events T_event is the ignition onset, so this window is the early-surge state, not true
rest — its log(WJI) is centred at ≈ +1.25, not 0. It is nonetheless (a) the window the
live gate uses to estimate sigma_log, and (b) the most symmetric/Gaussian of the candidate
windows (skew ≈ +0.23 vs heavy left-tailed pre-event windows). The true-rest (pre-event)
log(WJI) is heavily left-tailed (kurtosis ~10–14) because the buy-side kernel decays toward
0 during quiet periods, flooring WJI at ε. This is a known WJI structural quirk to revisit
if CUSUM mis-triggers; it does not block (sigma_log comes from the warmup window as specified).

Escalation (plan): pooled rest-state skewness > 1.5 or < −1.5 → HARD STOP (do not run T3).

Inputs
------
- results/phase_cpd/cpd0/wji_traces.pkl   (from T1)

Outputs
-------
- results/phase_cpd/cpd0/zero_background_report.json
- results/phase_cpd/cpd0/log_ratio_distributions.html
- results/phase_cpd/cpd0/sigma_log_summary.json

Run
---
    "D:/Trading Research/.venv/Scripts/python.exe" -m tools.phase_cpd.cpd0_t2_logratio
"""
from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

import numpy as np
from scipy import stats

import plotly.graph_objects as go
from plotly.subplots import make_subplots

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "results" / "phase_cpd" / "cpd0"
PKL_PATH = OUT_DIR / "wji_traces.pkl"

WARMUP_SEC = 300.0          # H0 window = [T_event, T_event + 300s active)
MIN_WARMUP_TICKS = 20       # below this, per-event sigma_log uses the fallback constant
SKEW_HARD_STOP = 1.5


def _rest_mask(tse: np.ndarray) -> np.ndarray:
    """Boolean mask for the warmup / H0 window: 0 ≤ active-sec-since-event < 300."""
    return (tse >= 0.0) & (tse < WARMUP_SEC)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows console defaults to cp1252
    except Exception:
        pass
    traces: dict[str, dict] = pickle.load(open(PKL_PATH, "rb"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Zero-background / undefined-log guard ──────────────────────────
    # Background ≡ 1.0 (never ≤ 0), and WJI is ε-floored (>0), so log is always
    # defined. We still scan and report, per the plan's T2 guard requirement.
    zero_bg_events = []
    total_ticks = 0
    total_bad = 0
    for key, t in traces.items():
        bg = t["wji_background"]
        wji = t["wji"]
        bad = int((bg <= 0).sum() + (wji <= 0).sum())
        total_ticks += t["n_ticks"]
        total_bad += bad
        if bad > 0:
            zero_bg_events.append({"event": key, "bad_ticks": bad, "n_ticks": t["n_ticks"]})
    zero_bg_report = {
        "note": "WJI_background ≡ 1.0 (Cooper Option A); WJI is ε-floored >0. "
                "log(WJI) is always defined. Guard scan included per plan T2.",
        "n_events": len(traces),
        "total_ticks": total_ticks,
        "events_with_bad_ticks": len(zero_bg_events),
        "total_bad_ticks": total_bad,
        "pct_events_with_bad_ticks": round(100.0 * len(zero_bg_events) / max(len(traces), 1), 3),
        "detail": zero_bg_events,
    }
    with open(OUT_DIR / "zero_background_report.json", "w") as f:
        json.dump(zero_bg_report, f, indent=2)
    # Plan hard stop: > 10% of events with zero-background ticks.
    if zero_bg_report["pct_events_with_bad_ticks"] > 10.0:
        print(f"HARD STOP (T2 zero-background): "
              f"{zero_bg_report['pct_events_with_bad_ticks']}% events have bad ticks.")
        return

    # ── Per-event sigma_log + pooled rest-state distribution (Window A) ──
    pooled_log = []
    per_event_sigma = []
    sigma_records = []
    event_rest = {}  # key → rest-window log array (for plotting)
    for key, t in traces.items():
        tse = t["t_since_event_active"]
        wji = t["wji"]
        m = _rest_mask(tse)
        w = wji[m]
        w = w[w > 0]
        if len(w) == 0:
            continue
        lw = np.log(w)
        event_rest[key] = lw
        pooled_log.append(lw)
        n = len(lw)
        if n >= MIN_WARMUP_TICKS:
            sig = float(lw.std(ddof=1))
            per_event_sigma.append(sig)
            sigma_records.append({"event": key, "n_warmup_ticks": n, "sigma_log": sig})
        else:
            sigma_records.append({"event": key, "n_warmup_ticks": n, "sigma_log": None})

    pooled = np.concatenate(pooled_log)
    skew = float(stats.skew(pooled))
    kurt = float(stats.kurtosis(pooled))   # excess kurtosis (Fisher)
    pooled_mean = float(pooled.mean())
    pooled_std = float(pooled.std(ddof=1))

    sig_arr = np.array(per_event_sigma, dtype=np.float64)
    sigma_summary = {
        "window": "warmup [T_event, T_event+300s active)  (proposal §8 / plan T2)",
        "n_events_with_sigma": int(len(sig_arr)),
        "n_events_below_min_ticks": int(sum(1 for r in sigma_records if r["sigma_log"] is None)),
        "min_warmup_ticks": MIN_WARMUP_TICKS,
        "sigma_log_mean": float(sig_arr.mean()),
        "sigma_log_std": float(sig_arr.std(ddof=1)),
        "sigma_log_p10": float(np.percentile(sig_arr, 10)),
        "sigma_log_median": float(np.median(sig_arr)),
        "sigma_log_p90": float(np.percentile(sig_arr, 90)),
        "recommended_fallback_constant": round(float(np.median(sig_arr)), 4),
        "fallback_rationale": "median per-event sigma_log across the 100 val events; used "
                              "when an event has < 20 warmup observations.",
        "pooled_rest_state": {
            "n_ticks": int(len(pooled)),
            "log_wji_mean": pooled_mean,
            "log_wji_std": pooled_std,
            "skewness": skew,
            "excess_kurtosis": kurt,
            "p10": float(np.percentile(pooled, 10)),
            "p50": float(np.percentile(pooled, 50)),
            "p90": float(np.percentile(pooled, 90)),
        },
        "skew_hard_stop_threshold": SKEW_HARD_STOP,
        "skew_within_bounds": bool(abs(skew) <= SKEW_HARD_STOP),
        "per_event": sigma_records,
    }
    with open(OUT_DIR / "sigma_log_summary.json", "w") as f:
        json.dump(sigma_summary, f, indent=2)

    # ── Distribution plot: pooled + 6 representative events ──────────────
    # Representative = 3 highest and 3 lowest n_ticks (thick vs thin names).
    by_ticks = sorted(traces.items(), key=lambda kv: kv[1]["n_ticks"])
    lowest3 = [k for k, _ in by_ticks[:3]]
    highest3 = [k for k, _ in by_ticks[-3:]]
    rep = highest3 + lowest3

    fig = make_subplots(
        rows=4, cols=2,
        subplot_titles=["POOLED rest-state log(WJI) — all 100 events"] + [""] + [
            f"{k}  (n_ticks={traces[k]['n_ticks']})" for k in rep
        ],
        specs=[[{"colspan": 2}, None]] + [[{}, {}] for _ in range(3)],
        vertical_spacing=0.09, horizontal_spacing=0.08,
    )

    def _hist_with_gauss(values, row, col, show_legend=False):
        values = np.asarray(values)
        fig.add_trace(
            go.Histogram(x=values, histnorm="probability density", nbinsx=80,
                         marker_color="#4C78A8", opacity=0.75,
                         name="log(WJI)", showlegend=show_legend),
            row=row, col=col,
        )
        mu, sd = float(values.mean()), float(values.std(ddof=1)) if len(values) > 1 else 1.0
        xs = np.linspace(values.min(), values.max(), 200)
        ys = stats.norm.pdf(xs, mu, sd)
        fig.add_trace(
            go.Scatter(x=xs, y=ys, mode="lines", line=dict(color="#E45756", width=2),
                       name="Gaussian fit", showlegend=show_legend),
            row=row, col=col,
        )

    _hist_with_gauss(pooled, 1, 1, show_legend=True)
    for i, k in enumerate(rep):
        r = 2 + i // 2
        c = 1 + i % 2
        _hist_with_gauss(event_rest.get(k, np.array([0.0])), r, c)

    fig.update_layout(
        title=(f"CPD-0 T2 — Rest-state log(WJI) distributions (warmup window)<br>"
               f"<sub>pooled n={len(pooled):,} | mean={pooled_mean:.3f} std={pooled_std:.3f} "
               f"skew={skew:.3f} excess-kurt={kurt:.3f} | "
               f"skew within ±{SKEW_HARD_STOP}: {abs(skew) <= SKEW_HARD_STOP}</sub>"),
        height=1100, width=1200, bargap=0.02, template="plotly_white",
    )
    fig.write_html(str(OUT_DIR / "log_ratio_distributions.html"), include_plotlyjs="cdn")

    # ── Report ──────────────────────────────────────────────────────────
    print("=" * 64)
    print("CPD-0 T2 — log-ratio transform + symmetry verification")
    print("=" * 64)
    print(f"WJI_background ≡ 1.0 (Cooper Option A)  →  WJI_log = log(WJI)")
    print(f"Zero-background / undefined-log ticks: {total_bad} "
          f"({zero_bg_report['pct_events_with_bad_ticks']}% events)  → no hard stop")
    print(f"\nPooled rest-state (warmup window) log(WJI):")
    print(f"  n={len(pooled):,}  mean={pooled_mean:.3f}  std={pooled_std:.3f}")
    print(f"  skewness={skew:.3f}  excess-kurtosis={kurt:.3f}")
    print(f"  ESCALATION: |skew| ≤ {SKEW_HARD_STOP}? {abs(skew) <= SKEW_HARD_STOP}  "
          f"→ {'PROCEED to T3' if abs(skew) <= SKEW_HARD_STOP else 'HARD STOP'}")
    print(f"\nPer-event sigma_log (n={len(sig_arr)} events ≥ {MIN_WARMUP_TICKS} ticks):")
    print(f"  mean={sigma_summary['sigma_log_mean']:.3f}  median={sigma_summary['sigma_log_median']:.3f}"
          f"  p10={sigma_summary['sigma_log_p10']:.3f}  p90={sigma_summary['sigma_log_p90']:.3f}")
    print(f"  recommended fallback constant = {sigma_summary['recommended_fallback_constant']}")
    print(f"  events below {MIN_WARMUP_TICKS} warmup ticks: "
          f"{sigma_summary['n_events_below_min_ticks']}")
    print(f"\nWritten:")
    print(f"  → {OUT_DIR / 'zero_background_report.json'}")
    print(f"  → {OUT_DIR / 'sigma_log_summary.json'}")
    print(f"  → {OUT_DIR / 'log_ratio_distributions.html'}")


if __name__ == "__main__":
    main()
