#!/usr/bin/env python3
"""
Phase EPG-Rapid-Tail-Risk — T0-T5 diagnostic charts (chart-first).

Reference config: sym_p80 on val_r4_stratified, T_gate=500s.
Writes Plotly HTML to results/phase_tail_risk/charts/ plus tail_trades.json,
premarket_mode_split.json, and a findings.json consumed by the summary.
No backtest re-run; tape reads are read-only auxiliary computation.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import tail_risk_lib as L

OUT = L.RESULTS / "phase_tail_risk"
CHARTS = OUT / "charts"
FINDINGS = {}


def _save(fig, relpath):
    p = CHARTS / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(p), include_plotlyjs="cdn")
    print(f"  wrote {relpath}")


# ══════════════════════════ T0 ══════════════════════════
def t0(df):
    print("\n[T0] stratum x session cross-tab")
    cells = []
    for s in ["low", "mid", "high"]:
        for sess in ["regular_hours", "pre_market"]:
            sub = df[(df.stratum == s) & (df.session_bucket == sess)]
            if len(sub) == 0:
                continue
            cells.append(dict(stratum=s, session=sess, n=len(sub),
                              pf=L.pf(sub.pnl_pct), wr=L.wr(sub.pnl_pct),
                              mean=float(sub.pnl_pct.mean()),
                              cvar5=L.cvar5(sub.pnl_pct) if len(sub) >= 10 else None))
    ct = pd.DataFrame(cells)
    FINDINGS["t0_crosstab"] = cells

    # reconcile blended
    recon = []
    for s in ["low", "mid", "high"]:
        sub = df[df.stratum == s]
        recon.append(dict(stratum=s, n=len(sub), pf=round(L.pf(sub.pnl_pct), 4),
                          wr=round(L.wr(sub.pnl_pct), 1)))
    FINDINGS["t0_blended"] = recon
    print("  blended:", recon)

    # Chart 1 — grouped bar PF & WR per cell
    ct["cell"] = ct.stratum.str.capitalize() + " · " + ct.session.map(L.SESSION_LABELS)
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(x=ct.cell, y=ct.pf, name="Profit Factor",
                         marker_color="#42A5F5",
                         text=[f"{v:.2f}<br>n={n}" for v, n in zip(ct.pf, ct.n)],
                         textposition="outside"), secondary_y=False)
    fig.add_trace(go.Scatter(x=ct.cell, y=ct.wr, name="Win Rate %", mode="markers",
                             marker=dict(color="#FFB74D", size=13, symbol="diamond")),
                  secondary_y=True)
    fig.add_hline(y=1.0, line=dict(color="#888", dash="dash"), secondary_y=False)
    fig.update_layout(**L.base_layout(
        "T0 · Stratum × Session — PF (bars) + WR% (diamonds) · sym_p80",
        "", "Profit Factor"))
    fig.update_yaxes(title_text="Win Rate %", secondary_y=True, range=[0, 100])
    _save(fig, "stratum_session_bar.html")

    # Chart 2 — PnL KDE by stratum, RTH & pre-market panels
    fig = make_subplots(rows=1, cols=2, subplot_titles=("RTH", "Pre-Market"),
                        shared_yaxes=True)
    allp = df.pnl_pct.values
    xs = np.linspace(allp.min() - 3, allp.max() + 3, 500)
    for ci, sess in enumerate(["regular_hours", "pre_market"], start=1):
        for s in ["low", "mid", "high"]:
            sub = df[(df.stratum == s) & (df.session_bucket == sess)]
            col = L.STRATUM_COLORS[s]
            if len(sub) >= 3:
                ys = L.kde_xy(sub.pnl_pct, xs)
                fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", line=dict(color=col, width=2),
                              name=f"{s} (n={len(sub)})", legendgroup=s,
                              showlegend=(ci == 1)), row=1, col=ci)
            # rug for every group (small-n honesty)
            fig.add_trace(go.Scatter(x=sub.pnl_pct, y=[-0.002 * (["low", "mid", "high"].index(s) + 1)] * len(sub),
                          mode="markers", marker=dict(color=col, size=6, symbol="line-ns-open"),
                          showlegend=False), row=1, col=ci)
    fig.add_vline(x=0, line=dict(color="#888", dash="dash"))
    fig.update_layout(**L.base_layout(
        "T0 · PnL% KDE by Stratum (rug = actual trades) · sym_p80", "PnL%", "Density"))
    _save(fig, "stratum_session_kde.html")


# ══════════════════════════ T1 ══════════════════════════
def t1(df):
    print("\n[T1] CVaR5 tail set")
    tail = df[df.in_cvar5_tail].sort_values("pnl_pct")
    dec = df[df.in_bottom_decile].sort_values("pnl_pct")
    recs = []
    for _, r in dec.iterrows():
        recs.append(dict(ticker=r.ticker, date=r.date, stratum=r.stratum,
                         mom_pct=round(r.mom_pct, 2), gap_pct_at_hit=round(r.gap_pct_at_hit, 2),
                         prev_close=round(r.prev_close, 4), session=r.session_bucket,
                         entry_lag_sec=round(r.entry_lag_sec, 1), hold_sec=round(r.hold_sec, 1),
                         pnl_pct=round(r.pnl_pct, 3), halt_overlap=bool(r.halt_overlap),
                         in_cvar5_tail=bool(r.in_cvar5_tail)))
    json.dump({"cvar5_pct": round(df.attrs["cvar5_pct"], 4),
               "cvar_n": df.attrs["cvar_n"], "decile_n": df.attrs["dec_n"],
               "records": recs}, open(OUT / "tail_trades.json", "w"), indent=2)
    FINDINGS["t1_tail"] = recs
    print(f"  tail n={len(tail)} (CVaR5={df.attrs['cvar5_pct']:.2f}%), decile n={len(dec)}")

    allp = df.pnl_pct.values
    xs = np.linspace(allp.min() - 3, allp.max() + 3, 500)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=xs, y=L.kde_xy(allp, xs), mode="lines",
                  line=dict(color="#78909C", width=2), fill="tozeroy",
                  fillcolor=L.hex_to_rgba("#78909C", 0.10),
                  name=f"All trades (n={len(df)})"))
    rest = df[~df.in_bottom_decile]
    fig.add_trace(go.Scatter(x=rest.pnl_pct, y=[0.002] * len(rest), mode="markers",
                  marker=dict(color="#B0BEC5", size=6, symbol="line-ns-open"),
                  name="rest"))
    fig.add_trace(go.Scatter(x=dec.pnl_pct, y=[0.006] * len(dec), mode="markers",
                  marker=dict(color="#FFA726", size=11, symbol="triangle-down"),
                  name=f"bottom decile (n={len(dec)})",
                  text=dec.ticker, hovertemplate="%{text}: %{x:.1f}%<extra></extra>"))
    fig.add_trace(go.Scatter(x=tail.pnl_pct, y=[0.010] * len(tail), mode="markers",
                  marker=dict(color="#EF5350", size=15, symbol="x"),
                  name=f"CVaR5 tail (n={len(tail)})",
                  text=tail.ticker, hovertemplate="%{text}: %{x:.1f}%<extra></extra>"))
    fig.add_vline(x=df.attrs["cvar5_pct"], line=dict(color="#EF5350", dash="dot"),
                  annotation_text=f"CVaR5={df.attrs['cvar5_pct']:.1f}%")
    fig.add_vline(x=0, line=dict(color="#888", dash="dash"))
    fig.update_layout(**L.base_layout(
        "T1 · Full-sample PnL% with CVaR5 tail + bottom decile marked · sym_p80",
        "PnL%", "Density"))
    _save(fig, "global_pnl_kde_tail.html")


# ══════════════════════════ T2 ══════════════════════════
def t2(df):
    print("\n[T2] pre-market bimodal split")
    boundary, assign, info = L.premarket_mode_split(df)
    FINDINGS["t2_gmm"] = info
    print("  GMM:", info)

    pm = df[df.session_bucket == "pre_market"].merge(assign[["ticker", "date", "pm_mode"]],
                                                     on=["ticker", "date"], how="left")
    loser_keys = set(zip(pm[pm.pm_mode == "loser"].ticker, pm[pm.pm_mode == "loser"].date))
    json.dump({"info": info,
               "assignment": [dict(ticker=r.ticker, date=r.date, pnl_pct=round(r.pnl_pct, 3),
                                   pm_mode=r.pm_mode) for _, r in pm.iterrows()]},
              open(OUT / "premarket_mode_split.json", "w"), indent=2)

    # Chart 1 (T2a) — premarket KDE + boundary + tail + low-stratum markers (three-way)
    x = pm.pnl_pct.values
    xs = np.linspace(x.min() - 3, x.max() + 3, 500)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=xs, y=L.kde_xy(x, xs), mode="lines",
                  line=dict(color="#90A4AE", width=2), fill="tozeroy",
                  fillcolor=L.hex_to_rgba("#90A4AE", 0.10),
                  name=f"Pre-market PnL (n={len(pm)})"))
    # rug of all premarket trades colored by mode
    for mode, col in [("loser", "#EF5350"), ("winner", "#66BB6A")]:
        sub = pm[pm.pm_mode == mode]
        fig.add_trace(go.Scatter(x=sub.pnl_pct, y=[0.004] * len(sub), mode="markers",
                      marker=dict(color=col, size=8, symbol="line-ns-open"),
                      name=f"{mode} mode (n={len(sub)})"))
    tail_pm = pm[pm.in_cvar5_tail]
    fig.add_trace(go.Scatter(x=tail_pm.pnl_pct, y=[0.010] * len(tail_pm), mode="markers",
                  marker=dict(color="#FF1744", size=15, symbol="x"),
                  name=f"CVaR5 tail ∩ pre (n={len(tail_pm)})", text=tail_pm.ticker,
                  hovertemplate="%{text}: %{x:.1f}%<extra></extra>"))
    low_pm = pm[pm.stratum == "low"]
    fig.add_trace(go.Scatter(x=low_pm.pnl_pct, y=[0.013] * len(low_pm), mode="markers",
                  marker=dict(color="#2196F3", size=11, symbol="circle-open"),
                  name=f"low stratum ∩ pre (n={len(low_pm)})", text=low_pm.ticker,
                  hovertemplate="%{text}: %{x:.1f}%<extra></extra>"))
    fig.add_vline(x=boundary, line=dict(color="#FFEB3B", dash="dash"),
                  annotation_text=f"split={boundary:.1f}%")
    fig.add_vline(x=0, line=dict(color="#888", dash="dot"))
    fig.update_layout(**L.base_layout(
        f"T2a · Pre-market bimodal split (loser μ={info['loser_mean']:.1f}%, "
        f"winner μ={info['winner_mean']:.1f}%) · sym_p80", "PnL%", "Density"))
    _save(fig, "premarket_kde_threeway.html")

    # Chart 2 (T2b) — three-way Venn
    low = set(zip(df[df.stratum == "low"].ticker, df[df.stratum == "low"].date))
    tail = set(zip(df[df.in_cvar5_tail].ticker, df[df.in_cvar5_tail].date))
    loser = loser_keys
    _venn3(low, tail, loser,
           ("Low stratum", "CVaR5 tail", "Pre-mkt loser mode"),
           ("#2196F3", "#EF5350", "#FFB74D"),
           "T2b · Three-way overlap · sym_p80", "threeway_venn.html")
    # overlap percentages
    n_low = len(low)
    FINDINGS["t2_overlap"] = dict(
        n_low=n_low, n_tail=len(tail), n_loser=len(loser),
        low_in_tail_pct=round(100 * len(low & tail) / n_low, 1) if n_low else 0,
        low_in_loser_pct=round(100 * len(low & loser) / n_low, 1) if n_low else 0,
        tail_in_loser_pct=round(100 * len(tail & loser) / len(tail), 1) if tail else 0,
        low_and_tail=len(low & tail), low_and_loser=len(low & loser),
        tail_and_loser=len(tail & loser), all_three=len(low & tail & loser))
    print("  overlap:", FINDINGS["t2_overlap"])


def _venn3(A, B, C, labels, colors, title, fname):
    """3-circle proportional-position Venn with region counts (plotly shapes)."""
    regions = {
        "A": len(A - B - C), "B": len(B - A - C), "C": len(C - A - B),
        "AB": len((A & B) - C), "AC": len((A & C) - B), "BC": len((B & C) - A),
        "ABC": len(A & B & C),
    }
    fig = go.Figure()
    centers = {"A": (-0.5, 0.35), "B": (0.5, 0.35), "C": (0.0, -0.5)}
    r = 0.85
    for k, (cx, cy) in centers.items():
        col = colors["ABC".index(k)]
        fig.add_shape(type="circle", x0=cx - r, y0=cy - r, x1=cx + r, y1=cy + r,
                      line=dict(color=col, width=2), fillcolor=L.hex_to_rgba(col, 0.22))
    label_pos = {
        "A": (-0.9, 0.6), "B": (0.9, 0.6), "C": (0.0, -1.0),
        "AB": (0.0, 0.65), "AC": (-0.65, -0.25), "BC": (0.65, -0.25), "ABC": (0.0, 0.05),
    }
    for k, (x, y) in label_pos.items():
        fig.add_annotation(x=x, y=y, text=f"<b>{regions[k]}</b>", showarrow=False,
                           font=dict(size=16, color="#fff"))
    name_pos = {"A": (-1.15, 1.25), "B": (1.15, 1.25), "C": (0.0, -1.55)}
    for k, (x, y) in name_pos.items():
        fig.add_annotation(x=x, y=y, text=f"{labels['ABC'.index(k)]}<br>(n={len(globals()['_setmap'][k])})",
                           showarrow=False, font=dict(size=12, color=colors["ABC".index(k)]))
    fig.update_layout(template=L._DARK, title=dict(text=title, x=0.01, font=dict(size=14)),
                      width=760, height=680, showlegend=False,
                      xaxis=dict(visible=False, range=[-2, 2]),
                      yaxis=dict(visible=False, range=[-2, 2], scaleanchor="x"))
    _save(fig, fname)


# ══════════════════════════ T3 ══════════════════════════
def _comparisons(df):
    pm = df[df.session_bucket == "pre_market"]
    boundary, assign, _ = L.premarket_mode_split(df)
    pm = pm.merge(assign[["ticker", "date", "pm_mode"]], on=["ticker", "date"], how="left")
    return [
        ("cvar5tail", "CVaR5 tail", df[df.in_cvar5_tail], "rest", df[~df.in_cvar5_tail]),
        ("premode", "Pre loser", pm[pm.pm_mode == "loser"], "Pre winner", pm[pm.pm_mode == "winner"]),
        ("lowstrat", "Low stratum", df[df.stratum == "low"], "Mid/High", df[df.stratum != "low"]),
    ]


def t3(df):
    print("\n[T3] entry-time-knowable contrast")
    comps = _comparisons(df)

    # T3a categorical
    cat_feats = [("sub_dollar", "Sub-$1 flag"), ("session_bucket", "Session"),
                 ("pm_tod_bucket", "Time-of-day bucket")]
    for fk, flabel in cat_feats:
        for cid, la, A, lb, B in comps:
            if fk == "session_bucket" and cid == "premode":
                continue  # premarket-only comparison, session is constant
            fig = go.Figure()
            cats = sorted(set(A[fk].astype(str)) | set(B[fk].astype(str)))
            for grp, lab, col in [(A, la, L.GROUP_COLORS["A"]), (B, lb, L.GROUP_COLORS["B"])]:
                vc = grp[fk].astype(str).value_counts(normalize=True) * 100
                fig.add_trace(go.Bar(x=cats, y=[vc.get(c, 0) for c in cats],
                              name=f"{lab} (n={len(grp)})", marker_color=col))
            fig.update_layout(**L.base_layout(
                f"T3a · {flabel} — {la} vs {lb}", flabel, "% of group"), barmode="group")
            _save(fig, f"t3a_categorical_bars/{fk}__{cid}.html")

    # T3b continuous
    cont_feats = [("gap_pct_at_hit", "Gap % at scanner hit"), ("prev_close", "Prev close $"),
                  ("entry_lag_sec", "Entry lag (s)"), ("n_trades_before_scanner", "Trades before scanner"),
                  ("pre60_count", "Trades in 60s pre-entry"), ("pre60_mean_size", "Mean size 60s pre-entry")]
    verdicts = {}
    for fk, flabel in cont_feats:
        for cid, la, A, lb, B in comps:
            a = A[fk].dropna().values
            b = B[fk].dropna().values
            if len(a) < 2 or len(b) < 2:
                continue
            logx = fk in ("n_trades_before_scanner", "pre60_count", "pre60_mean_size")
            xa = np.log10(np.clip(a, 1, None)) if logx else a
            xb = np.log10(np.clip(b, 1, None)) if logx else b
            lo = min(xa.min(), xb.min())
            hi = max(xa.max(), xb.max())
            xs = np.linspace(lo - 0.05 * (hi - lo + 1e-9), hi + 0.05 * (hi - lo + 1e-9), 400)
            fig = go.Figure()
            for xd, lab, col, raw in [(xa, la, L.GROUP_COLORS["A"], a), (xb, lb, L.GROUP_COLORS["B"], b)]:
                ys = L.kde_xy(xd, xs)
                if ys is not None and len(xd) >= 3:
                    fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", line=dict(color=col, width=2),
                                  fill="tozeroy", fillcolor=L.hex_to_rgba(col, 0.08),
                                  name=f"{lab} (n={len(xd)}, med={np.median(raw):.3g})"))
                fig.add_trace(go.Scatter(x=xd, y=[-0.01 * ys.max() if ys is not None else 0] * len(xd),
                              mode="markers", marker=dict(color=col, size=7, symbol="line-ns-open"),
                              showlegend=False))
            # simple separation metric (Mann-Whitney rank-biserial proxy)
            import scipy.stats as sps
            try:
                u, pval = sps.mannwhitneyu(a, b, alternative="two-sided")
                sep = abs(2 * u / (len(a) * len(b)) - 1)
            except Exception:
                pval, sep = np.nan, np.nan
            verdicts[(fk, cid)] = dict(feature=flabel, comparison=f"{la} vs {lb}",
                                       med_A=float(np.median(a)), med_B=float(np.median(b)),
                                       n_A=len(a), n_B=len(b),
                                       mwu_p=round(float(pval), 4), effect=round(float(sep), 3))
            xt = f"log10({flabel})" if logx else flabel
            fig.update_layout(**L.base_layout(
                f"T3b · {flabel} — {la} vs {lb}  (MWU p={pval:.3f}, effect={sep:.2f})",
                xt, "Density"))
            _save(fig, f"t3b_continuous_kde/{fk}__{cid}.html")
    FINDINGS["t3b_verdicts"] = [dict(k0=k[0], k1=k[1], **v) for k, v in verdicts.items()]

    # T3d proxy scatter gap vs mom
    g = df.dropna(subset=["gap_pct_at_hit", "mom_pct"])
    import scipy.stats as sps
    pear = sps.pearsonr(g.gap_pct_at_hit, g.mom_pct)
    spear = sps.spearmanr(g.gap_pct_at_hit, g.mom_pct)
    fig = go.Figure()
    for s in ["low", "mid", "high"]:
        sub = g[g.stratum == s]
        fig.add_trace(go.Scatter(x=sub.gap_pct_at_hit, y=sub.mom_pct, mode="markers",
                      marker=dict(color=L.STRATUM_COLORS[s], size=8), name=s,
                      text=sub.ticker, hovertemplate="%{text}<br>gap=%{x:.1f} mom=%{y:.1f}<extra></extra>"))
    lx, ly = L.lowess(g.gap_pct_at_hit.values, g.mom_pct.values, frac=0.7)
    fig.add_trace(go.Scatter(x=lx, y=ly, mode="lines", line=dict(color="#fff", width=2, dash="dash"),
                  name="LOWESS"))
    fig.update_layout(**L.base_layout(
        f"T3d · gap_pct_at_hit vs mom_pct  (Pearson r={pear[0]:.2f} p={pear[1]:.3f}; "
        f"Spearman ρ={spear[0]:.2f})", "gap_pct_at_hit (entry-knowable)", "mom_pct (retrospective)"))
    _save(fig, "t3d_gap_mom_scatter.html")
    FINDINGS["t3d"] = dict(pearson_r=round(float(pear[0]), 3), pearson_p=round(float(pear[1]), 4),
                           spearman_rho=round(float(spear[0]), 3))
    print(f"  T3d gap~mom: Pearson r={pear[0]:.3f}, Spearman ρ={spear[0]:.3f}")


# ══════════════════════════ T3e ══════════════════════════
def t3e(df):
    print("\n[T3e] cumulative volume at entry (Cooper inverted-U)")
    v = df.dropna(subset=["cum_dollar_vol_at_entry"]).copy()
    v["logvol"] = np.log10(v.cum_dollar_vol_at_entry.clip(lower=1))

    # Chart 1 — KDE of cum vol by stratum
    xs = np.linspace(v.logvol.min() - 0.3, v.logvol.max() + 0.3, 400)
    fig = go.Figure()
    for s in ["low", "mid", "high"]:
        sub = v[v.stratum == s]
        if len(sub) >= 3:
            fig.add_trace(go.Scatter(x=xs, y=L.kde_xy(sub.logvol, xs), mode="lines",
                          line=dict(color=L.STRATUM_COLORS[s], width=2),
                          name=f"{s} (n={len(sub)}, med=${10**np.median(sub.logvol):,.0f})"))
        fig.add_trace(go.Scatter(x=sub.logvol, y=[-0.02] * len(sub), mode="markers",
                      marker=dict(color=L.STRATUM_COLORS[s], size=6, symbol="line-ns-open"),
                      showlegend=False))
    fig.update_layout(**L.base_layout(
        "T3e·1 · Cumulative $volume at entry by stratum", "log10($ volume at entry)", "Density"))
    _save(fig, "t3e_volume_kde_by_stratum.html")

    # Chart 2 — quintile bins: PF / WR / mean PnL
    v["qbin"] = pd.qcut(v.cum_dollar_vol_at_entry, 5, labels=["Q1", "Q2", "Q3", "Q4", "Q5"])
    agg = []
    for q in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
        sub = v[v.qbin == q]
        agg.append(dict(q=q, n=len(sub), pf=L.pf(sub.pnl_pct), wr=L.wr(sub.pnl_pct),
                        mean=float(sub.pnl_pct.mean()),
                        vlo=sub.cum_dollar_vol_at_entry.min(), vhi=sub.cum_dollar_vol_at_entry.max()))
    ag = pd.DataFrame(agg)
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(x=ag.q, y=ag.pf, name="PF", marker_color="#42A5F5",
                  text=[f"PF {p:.2f}<br>n={n}<br>{v/1e6:.1f}–{h/1e6:.1f}M" for p, n, v, h in
                        zip(ag.pf, ag.n, ag.vlo, ag.vhi)], textposition="outside"), secondary_y=False)
    fig.add_trace(go.Scatter(x=ag.q, y=ag["mean"], name="mean PnL%", mode="lines+markers",
                  line=dict(color="#FFB74D", width=2)), secondary_y=True)
    fig.add_hline(y=1.0, line=dict(color="#888", dash="dash"), secondary_y=False)
    fig.update_layout(**L.base_layout(
        "T3e·2 · Cum $volume quintiles → PF (bars) & mean PnL% (line)",
        "Volume quintile (low→high)", "Profit Factor"), barmode="group")
    fig.update_yaxes(title_text="mean PnL%", secondary_y=True)
    _save(fig, "t3e_volume_bins_bar.html")
    FINDINGS["t3e_bins"] = agg
    shape = _describe_shape(ag.pf.values)
    FINDINGS["t3e_shape"] = shape
    print(f"  T3e volume→PF shape: {shape}")

    # Chart 3 — vol vs mom scatter (proxy)
    import scipy.stats as sps
    sp = sps.spearmanr(v.logvol, v.mom_pct)
    fig = go.Figure()
    for s in ["low", "mid", "high"]:
        sub = v[v.stratum == s]
        fig.add_trace(go.Scatter(x=sub.logvol, y=sub.mom_pct, mode="markers",
                      marker=dict(color=L.STRATUM_COLORS[s], size=8), name=s,
                      text=sub.ticker, hovertemplate="%{text}<extra></extra>"))
    lx, ly = L.lowess(v.logvol.values, v.mom_pct.values, frac=0.7)
    fig.add_trace(go.Scatter(x=lx, y=ly, mode="lines", line=dict(color="#fff", dash="dash"), name="LOWESS"))
    fig.update_layout(**L.base_layout(
        f"T3e·3 · Cum $volume vs mom_pct (Spearman ρ={sp[0]:.2f})",
        "log10($ volume at entry)", "mom_pct"))
    _save(fig, "t3e_volume_mom_scatter.html")

    # Chart 4 — vol vs PnL, LOWESS, faceted by stratum (headline)
    fig = make_subplots(rows=1, cols=3, subplot_titles=("Low", "Mid", "High"),
                        shared_yaxes=True)
    for ci, s in enumerate(["low", "mid", "high"], start=1):
        sub = v[v.stratum == s]
        fig.add_trace(go.Scatter(x=sub.logvol, y=sub.pnl_pct, mode="markers",
                      marker=dict(color=L.STRATUM_COLORS[s], size=8), showlegend=False,
                      text=sub.ticker, hovertemplate="%{text}<br>%{y:.1f}%<extra></extra>"), row=1, col=ci)
        if len(sub) >= 5:
            lx, ly = L.lowess(sub.logvol.values, sub.pnl_pct.values, frac=0.8)
            fig.add_trace(go.Scatter(x=lx, y=ly, mode="lines", line=dict(color="#fff", dash="dash"),
                          showlegend=False), row=1, col=ci)
        fig.add_hline(y=0, line=dict(color="#888", dash="dot"), row=1, col=ci)
    # pooled lowess overlaid on each? keep faceted only
    fig.update_layout(**L.base_layout(
        "T3e·4 · Cum $volume at entry vs PnL% — faceted by stratum (LOWESS)",
        "log10($ volume at entry)", "PnL%"))
    _save(fig, "t3e_volume_pnl_scatter.html")


def _describe_shape(y):
    y = np.asarray(y, dtype=float)
    if np.all(np.diff(y) > 0):
        return "monotonic increasing"
    if np.all(np.diff(y) < 0):
        return "monotonic decreasing"
    peak = int(np.argmax(y))
    trough = int(np.argmin(y))
    if 0 < peak < len(y) - 1 and y[peak] > y[0] and y[peak] > y[-1]:
        return "inverted-U"
    if 0 < trough < len(y) - 1 and y[trough] < y[0] and y[trough] < y[-1]:
        return "U-shaped"
    return "no clear monotone/unimodal pattern"


# ══════════════════════════ T4 ══════════════════════════
def t4(df, traj):
    print("\n[T4] post-entry features")
    comps = _comparisons(df)

    # T4a — % crossing RTH open
    fig = go.Figure()
    labels, vals, cols = [], [], []
    for cid, la, A, lb, B in comps:
        for grp, lab, col in [(A, la, L.GROUP_COLORS["A"]), (B, lb, L.GROUP_COLORS["B"])]:
            labels.append(f"{lab}<br>({cid})")
            vals.append(100 * grp.crosses_rth_open.mean() if len(grp) else 0)
            cols.append(col)
    fig.add_trace(go.Bar(x=labels, y=vals, marker_color=cols,
                  text=[f"{v:.0f}%" for v in vals], textposition="outside"))
    fig.update_layout(**L.base_layout(
        "T4a · % of trades whose hold crosses 09:30 ET (RTH open)", "", "% crossing"))
    _save(fig, "t4a_rth_crossing_bar.html")

    # T4a2 — post-entry volume trajectory (median + IQR), 4 lines
    boundary, assign, _ = L.premarket_mode_split(df)
    pm = df[df.session_bucket == "pre_market"].merge(assign[["ticker", "date", "pm_mode"]],
                                                     on=["ticker", "date"], how="left")
    loser_keys = set(zip(pm[pm.pm_mode == "loser"].ticker, pm[pm.pm_mode == "loser"].date))
    winner_keys = set(zip(pm[pm.pm_mode == "winner"].ticker, pm[pm.pm_mode == "winner"].date))
    low_keys = set(zip(df[df.stratum == "low"].ticker, df[df.stratum == "low"].date))
    midhigh_keys = set(zip(df[df.stratum != "low"].ticker, df[df.stratum != "low"].date))

    groups = [("Low stratum", low_keys, "#2196F3"), ("Mid/High", midhigh_keys, "#26a69a"),
              ("Pre loser", loser_keys, "#EF5350"), ("Pre winner", winner_keys, "#66BB6A")]
    fig = go.Figure()
    grid = None
    for lab, keys, col in groups:
        series = [traj[k][1] for k in keys if k in traj]
        if not series:
            continue
        grid = traj[next(iter(keys & set(traj.keys())))][0]
        M = np.vstack(series)
        med = np.median(M, axis=0)
        q1, q3 = np.percentile(M, 25, axis=0), np.percentile(M, 75, axis=0)
        fig.add_trace(go.Scatter(x=np.concatenate([grid, grid[::-1]]),
                      y=np.concatenate([q3, q1[::-1]]), fill="toself",
                      fillcolor=L.hex_to_rgba(col, 0.10), line=dict(width=0),
                      showlegend=False, hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=grid, y=med, mode="lines", line=dict(color=col, width=2.5),
                      name=f"{lab} (n={len(series)})"))
    fig.update_layout(**L.base_layout(
        "T4a2 · Post-entry cumulative $volume (median + IQR), first 300s",
        "seconds since entry", "cumulative $ volume"))
    _save(fig, "t4a2_volume_trajectory.html")

    # T4b — hold duration KDE per comparison (small multiples in one fig via separate files)
    for cid, la, A, lb, B in comps:
        a, b = A.hold_sec.dropna().values, B.hold_sec.dropna().values
        if len(a) < 2 or len(b) < 2:
            continue
        lo = min(a.min(), b.min())
        hi = max(a.max(), b.max())
        xs = np.linspace(lo - 20, hi + 20, 400)
        fig = go.Figure()
        for xd, lab, col in [(a, la, L.GROUP_COLORS["A"]), (b, lb, L.GROUP_COLORS["B"])]:
            ys = L.kde_xy(xd, xs)
            if ys is not None and len(xd) >= 3:
                fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", line=dict(color=col, width=2),
                              fill="tozeroy", fillcolor=L.hex_to_rgba(col, 0.08),
                              name=f"{lab} (n={len(xd)}, med={np.median(xd):.0f}s)"))
            fig.add_trace(go.Scatter(x=xd, y=[0] * len(xd), mode="markers",
                          marker=dict(color=col, size=7, symbol="line-ns-open"), showlegend=False))
        fig.update_layout(**L.base_layout(
            f"T4b · Hold duration — {la} vs {lb}", "hold (s)", "Density"))
        _save(fig, f"t4b_hold_duration_kde__{cid}.html")


# ══════════════════════════ T5 ══════════════════════════
def t5():
    print("\n[T5] stability across p sweep")
    ps = ["p65", "p70", "p75", "p80", "p85", "p90"]
    rows = []
    for pt in ps:
        try:
            d = L.load_joined(pt)
        except Exception as e:
            print(f"  skip {pt}: {e}")
            continue
        tail = set(zip(d[d.in_cvar5_tail].ticker, d[d.in_cvar5_tail].date))
        low = set(zip(d[d.stratum == "low"].ticker, d[d.stratum == "low"].date))
        rows.append(dict(p=int(pt[1:]) / 100, tail_n=len(tail),
                         overlap_pct=round(100 * len(tail & low) / len(tail), 1) if tail else 0,
                         cvar5=round(d.attrs["cvar5_pct"], 2)))
    r = pd.DataFrame(rows)
    FINDINGS["t5"] = rows
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=r.p, y=r.tail_n, mode="lines+markers", name="CVaR5 tail set size",
                  line=dict(color="#42A5F5", width=2)), secondary_y=False)
    fig.add_trace(go.Scatter(x=r.p, y=r.overlap_pct, mode="lines+markers",
                  name="% tail ∩ low stratum", line=dict(color="#EF5350", width=2)), secondary_y=True)
    fig.update_layout(**L.base_layout(
        "T5 · Tail-set stability across gate threshold", "gate p (p_open=p_close)",
        "CVaR5 tail set size"))
    fig.update_yaxes(title_text="% tail ∩ low", secondary_y=True, range=[0, 100])
    _save(fig, "t5_stability_line.html")
    print("  ", rows)


def main():
    CHARTS.mkdir(parents=True, exist_ok=True)
    df = L.load_joined("p80")
    df = L.compute_tape_features(df)
    print("Building post-entry trajectories (tape)...")
    traj = L.post_entry_volume_trajectory(df, horizon_sec=300, step_sec=10)

    # expose set-map for venn labels
    globals()["_setmap"] = {
        "A": set(zip(df[df.stratum == "low"].ticker, df[df.stratum == "low"].date)),
        "B": set(zip(df[df.in_cvar5_tail].ticker, df[df.in_cvar5_tail].date)),
        "C": set(),  # filled in t2
    }

    t0(df)
    t1(df)
    # fill loser set for venn label C before t2 draws (t2 recomputes)
    _, assign, _ = L.premarket_mode_split(df)
    globals()["_setmap"]["C"] = set(zip(assign[assign.pm_mode == "loser"].ticker,
                                        assign[assign.pm_mode == "loser"].date))
    t2(df)
    t3(df)
    t3e(df)
    t4(df, traj)
    t5()

    json.dump(FINDINGS, open(OUT / "findings.json", "w"), indent=2, default=str)
    print(f"\nFindings -> {OUT / 'findings.json'}")
    print("Done T0-T5.")


if __name__ == "__main__":
    main()
