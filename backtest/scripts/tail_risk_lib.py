#!/usr/bin/env python3
"""
Phase EPG-Rapid-Tail-Risk — shared analysis data layer + helpers.

Everything downstream (T0-T5 charts, JSON records, per-event union set) draws
from `load_joined()`. No backtest re-run: reads the R1-Final per_trade.json on
disk and joins the val_r4_stratified sample for stratum / mom_pct /
gap_pct_at_hit / scanner_hit_idx. Cumulative volume (T3e/T4a2) reads the trade
tape via data.loaders.trades (read-only), which is not a backtest run.

CVaR5 definition mirrors runner_rapid.py exactly:
    sorted_pnl = np.sort(pnl); cvar_n = max(1, int(0.05*n)); mean(sorted[:cvar_n]).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import scipy.stats as spstats

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BACKTEST = PROJECT_ROOT / "backtest"
RESULTS = BACKTEST / "results"
SAMPLE = BACKTEST / "data" / "val_r4_stratified.json"

# make `data.loaders.trades` importable (package root is backtest/)
if str(BACKTEST) not in sys.path:
    sys.path.insert(0, str(BACKTEST))

RTH_OPEN_SEC = 34200      # 09:30 ET
RTH_CLOSE_SEC = 57600     # 16:00 ET
NS = 1_000_000_000

STRATUM_COLORS = {"low": "#2196F3", "mid": "#FF9800", "high": "#F44336"}
SESSION_COLORS = {"regular_hours": "#26a69a", "pre_market": "#FFB74D", "post_market": "#CE93D8"}
SESSION_LABELS = {"regular_hours": "RTH", "pre_market": "Pre-Market", "post_market": "Post-Market"}
GROUP_COLORS = {"A": "#EF5350", "B": "#42A5F5"}  # contrast group A (tail/loser/low) vs B (rest)

_DARK = "plotly_dark"
_W, _H = 1050, 600


# ───────────────────────── data layer ─────────────────────────
def _sample_index() -> dict:
    evs = json.load(open(SAMPLE))["events"]
    return {(e["ticker"], e["date"]): e for e in evs}


def load_joined(p_tag: str = "p80") -> pd.DataFrame:
    """Join sym_<p_tag> per_trade with the stratified sample and derive fields."""
    per_trade = RESULTS / "phase_r1_final" / f"sym_{p_tag}" / "per_trade.json"
    trades = json.load(open(per_trade))
    smp = _sample_index()

    rows = []
    for t in trades:
        e = smp.get((t["ticker"], t["date"]), {})
        rows.append({
            **t,
            "stratum": e.get("stratum", "unknown"),
            "mom_pct": e.get("mom_pct", np.nan),
            "gap_pct_at_hit": e.get("gap_pct_at_hit", np.nan),
            "scanner_hit_price": e.get("scanner_hit_price", np.nan),
            "scanner_hit_idx": e.get("scanner_hit_idx", np.nan),
            "scanner_hit_tod_sec": e.get("scanner_hit_tod_sec", np.nan),
        })
    df = pd.DataFrame(rows)

    for c in ["entry_ts", "exit_ts", "entry_t_sec", "exit_t_sec", "hold_sec",
              "entry_lag_sec", "entry_lag_from_scanner_sec", "pnl_pct",
              "time_of_day_sec", "prev_close", "entry_price", "mom_pct",
              "gap_pct_at_hit", "scanner_hit_idx", "n_halt_windows"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # derived, entry-time-knowable
    df["sub_dollar"] = df["entry_price"] < 1.0
    df["n_trades_before_scanner"] = df["scanner_hit_idx"]   # tape index at scanner hit
    df["halt_overlap"] = df["n_halt_windows"] > 0
    df["crosses_rth_open"] = (df["entry_t_sec"] < RTH_OPEN_SEC) & (df["exit_t_sec"] >= RTH_OPEN_SEC)

    def _pm_bucket(s):
        if s < 21600:
            return "04-06"
        if s < 28800:
            return "06-08"
        if s < RTH_OPEN_SEC:
            return "08-09:30"
        return "RTH+"
    df["pm_tod_bucket"] = df["time_of_day_sec"].apply(_pm_bucket)

    # tail / decile membership (mirror runner CVaR5)
    n = len(df)
    cvar_n = max(1, int(0.05 * n))
    dec_n = max(1, int(0.10 * n))
    order = df["pnl_pct"].values.argsort()
    tail_idx = set(df.index[order[:cvar_n]])
    dec_idx = set(df.index[order[:dec_n]])
    df["in_cvar5_tail"] = df.index.isin(tail_idx)
    df["in_bottom_decile"] = df.index.isin(dec_idx)
    df.attrs["cvar_n"] = cvar_n
    df.attrs["dec_n"] = dec_n
    df.attrs["cvar5_pct"] = float(np.mean(np.sort(df["pnl_pct"].values)[:cvar_n]))
    df.attrs["p_tag"] = p_tag
    return df


def premarket_mode_split(df: pd.DataFrame):
    """2-component GMM on pre-market PnL%. Returns (boundary, assignment, info)."""
    from sklearn.mixture import GaussianMixture
    pm = df[df["session_bucket"] == "pre_market"].copy()
    x = pm["pnl_pct"].values.reshape(-1, 1)

    gmm2 = GaussianMixture(n_components=2, random_state=42, n_init=10).fit(x)
    gmm1 = GaussianMixture(n_components=1, random_state=42).fit(x)
    means = gmm2.means_.ravel()
    lo_comp = int(np.argmin(means))
    hi_comp = 1 - lo_comp

    # decision boundary = crossover of the two weighted gaussians on a fine grid
    grid = np.linspace(x.min() - 2, x.max() + 2, 4000).reshape(-1, 1)
    resp = gmm2.predict_proba(grid)
    hi_post = resp[:, hi_comp]
    # first grid point (scanning up) where hi-component posterior exceeds 0.5
    above = np.where(hi_post >= 0.5)[0]
    boundary = float(grid[above[0], 0]) if len(above) else float(means.mean())

    labels = gmm2.predict(x)
    pm["pm_mode"] = ["winner" if l == hi_comp else "loser" for l in labels]
    info = {
        "n_premarket": int(len(pm)),
        "comp_means": [round(float(m), 3) for m in means],
        "comp_weights": [round(float(w), 3) for w in gmm2.weights_],
        "comp_sigmas": [round(float(np.sqrt(c)), 3) for c in gmm2.covariances_.ravel()],
        "loser_mean": round(float(means[lo_comp]), 3),
        "winner_mean": round(float(means[hi_comp]), 3),
        "boundary_pnl_pct": round(boundary, 3),
        "n_loser": int((pm["pm_mode"] == "loser").sum()),
        "n_winner": int((pm["pm_mode"] == "winner").sum()),
        "bic_2comp": round(float(gmm2.bic(x)), 2),
        "bic_1comp": round(float(gmm1.bic(x)), 2),
        "two_comp_preferred": bool(gmm2.bic(x) < gmm1.bic(x)),
    }
    return boundary, pm[["ticker", "date", "pnl_pct", "pm_mode"]], info


# ───────────────────────── stats helpers ─────────────────────────
def pf(pnl) -> float:
    p = np.asarray(pnl, dtype=float)
    w = p[p > 0].sum()
    l = abs(p[p < 0].sum())
    return float(w / l) if l > 0 else float("inf")


def wr(pnl) -> float:
    p = np.asarray(pnl, dtype=float)
    return float(100 * (p > 0).mean()) if len(p) else float("nan")


def cvar5(pnl) -> float:
    p = np.sort(np.asarray(pnl, dtype=float))
    n = len(p)
    cn = max(1, int(0.05 * n))
    return float(np.mean(p[:cn])) if n else float("nan")


def kde_xy(data, xs, bw="scott"):
    data = np.asarray(data, dtype=float)
    if len(data) < 2 or np.ptp(data) == 0:
        return None
    return spstats.gaussian_kde(data, bw_method=bw)(xs)


def lowess(x, y, frac=0.6, n_out=200):
    """Minimal LOWESS (locally weighted linear regression, tricube kernel)."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    order = np.argsort(x)
    x, y = x[order], y[order]
    n = len(x)
    if n < 4:
        return x, y
    r = int(np.ceil(frac * n))
    r = max(r, 3)
    xs_out = np.linspace(x.min(), x.max(), n_out)
    ys_out = np.empty(n_out)
    for i, x0 in enumerate(xs_out):
        d = np.abs(x - x0)
        h = np.sort(d)[min(r - 1, n - 1)]
        h = h if h > 0 else 1e-9
        w = np.clip(d / h, 0, 1)
        w = (1 - w ** 3) ** 3
        W = np.sum(w)
        if W <= 0:
            ys_out[i] = np.mean(y)
            continue
        mx = np.sum(w * x) / W
        my = np.sum(w * y) / W
        sxx = np.sum(w * (x - mx) ** 2)
        sxy = np.sum(w * (x - mx) * (y - my))
        b = sxy / sxx if sxx > 1e-12 else 0.0
        a = my - b * mx
        ys_out[i] = a + b * x0
    return xs_out, ys_out


def hex_to_rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


# ───────────────────────── tape / volume ─────────────────────────
def _mom_lookup():
    return {(e["ticker"], e["date"]): e["mom_pct"] for e in json.load(open(SAMPLE))["events"]}


def cum_volume_at_entry(df: pd.DataFrame) -> pd.DataFrame:
    """Cumulative dollar volume (Σ price·size) and trade count from 04:00 ET
    session start through each trade's entry_ts. Reads the trade tape."""
    from data.loaders.trades import load_trades

    mom = _mom_lookup()
    cvol, ccnt = [], []
    for _, row in df.iterrows():
        try:
            td = load_trades(row["ticker"], row["date"],
                             mom.get((row["ticker"], row["date"])))
            mask = td.timestamps <= int(row["entry_ts"])
            cvol.append(float(np.sum(td.prices[mask] * td.sizes[mask])))
            ccnt.append(int(np.sum(mask)))
        except Exception as e:
            print(f"    ! volume load failed {row['ticker']} {row['date']}: {e}")
            cvol.append(np.nan)
            ccnt.append(np.nan)
    out = df.copy()
    out["cum_dollar_vol_at_entry"] = cvol
    out["cum_trades_at_entry"] = ccnt
    return out


def compute_tape_features(df: pd.DataFrame, cache=True) -> pd.DataFrame:
    """One tape pass per event: cumulative $vol & trade count at entry, plus
    60s-pre-entry trade count and mean size. Cached to phase_tail_risk/."""
    cache_path = RESULTS / "phase_tail_risk" / "tape_features.json"
    if cache and cache_path.exists():
        cached = {tuple(k.split("|")): v for k, v in json.load(open(cache_path)).items()}
        if all((r.ticker, r.date) in cached for r in df.itertuples()):
            out = df.copy()
            for col in ["cum_dollar_vol_at_entry", "cum_trades_at_entry",
                        "pre60_count", "pre60_mean_size"]:
                out[col] = [cached[(r.ticker, r.date)][col] for r in df.itertuples()]
            print(f"  tape features: loaded from cache ({len(df)} rows)")
            return out

    from data.loaders.trades import load_trades
    mom = _mom_lookup()
    feats = {}
    for r in df.itertuples():
        rec = {"cum_dollar_vol_at_entry": np.nan, "cum_trades_at_entry": np.nan,
               "pre60_count": np.nan, "pre60_mean_size": np.nan}
        try:
            td = load_trades(r.ticker, r.date, mom.get((r.ticker, r.date)))
            e_ns = int(r.entry_ts)
            at = td.timestamps <= e_ns
            rec["cum_dollar_vol_at_entry"] = float(np.sum(td.prices[at] * td.sizes[at]))
            rec["cum_trades_at_entry"] = int(np.sum(at))
            pre = at & (td.timestamps >= e_ns - 60 * NS)
            rec["pre60_count"] = int(np.sum(pre))
            rec["pre60_mean_size"] = float(np.mean(td.sizes[pre])) if np.sum(pre) else 0.0
        except Exception as e:
            print(f"    ! tape features failed {r.ticker} {r.date}: {e}")
        feats[(r.ticker, r.date)] = rec

    if cache:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        json.dump({f"{k[0]}|{k[1]}": v for k, v in feats.items()},
                  open(cache_path, "w"), indent=1)
    out = df.copy()
    for col in ["cum_dollar_vol_at_entry", "cum_trades_at_entry", "pre60_count", "pre60_mean_size"]:
        out[col] = [feats[(r.ticker, r.date)][col] for r in df.itertuples()]
    print(f"  tape features: computed fresh ({len(df)} rows)")
    return out


def post_entry_volume_trajectory(df: pd.DataFrame, horizon_sec=300, step_sec=10):
    """Median cumulative dollar-volume trajectory in [entry, entry+horizon].
    Returns dict ticker,date -> (grid_sec, cum_dollar_vol array)."""
    from data.loaders.trades import load_trades

    mom = _mom_lookup()
    grid = np.arange(0, horizon_sec + step_sec, step_sec)
    out = {}
    for _, row in df.iterrows():
        try:
            td = load_trades(row["ticker"], row["date"],
                             mom.get((row["ticker"], row["date"])))
            e_ns = int(row["entry_ts"])
            rel = (td.timestamps - e_ns) / NS
            dv = td.prices * td.sizes
            traj = np.array([dv[(rel >= 0) & (rel <= g)].sum() for g in grid])
            out[(row["ticker"], row["date"])] = (grid, traj)
        except Exception as e:
            print(f"    ! trajectory load failed {row['ticker']} {row['date']}: {e}")
    return out


def base_layout(title, xtitle, ytitle, **kw):
    return dict(template=_DARK,
                title=dict(text=title, x=0.01, font=dict(size=14)),
                xaxis_title=xtitle, yaxis_title=ytitle,
                width=_W, height=_H,
                legend=dict(x=0.01, y=0.99, font=dict(size=10),
                            bgcolor="rgba(0,0,0,0.3)"),
                **kw)
