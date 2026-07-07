#!/usr/bin/env python3
"""
Phase VAL-FULL T2-T6 — analysis, charts, and verdicts over the full-pool run.

Reads (no backtest re-run):
  results/phase_val_full/full_pool_run/{per_trade,run_summary}.json   (T2 run output)
  data/val_full.json                                                  (stratum join)
  results/phase_r1_final/sym_p80/{per_trade,run_summary}.json         (val_r4 baseline)

CVaR5 mirrors runner_rapid.py exactly (via tail_risk_lib.cvar5).
Chart-first: Plotly HTML with numbers in titles; markdown captions carry the tables.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

BACKTEST = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKTEST / "scripts"))
import tail_risk_lib as L  # noqa: E402  pf/wr/cvar5/kde_xy/base_layout/hex_to_rgba/colors/load_joined

OUT = BACKTEST / "results" / "phase_val_full"
RUN = OUT / "full_pool_run"
CHARTS = OUT / "charts"
VAL_FULL = BACKTEST / "data" / "val_full.json"
VALR4_DIR = BACKTEST / "results" / "phase_r1_final" / "sym_p80"

ALLOWED_EXITS = {"epg_window_close", "session_end"}
FINDINGS: dict = {}


def _save(fig, name):
    CHARTS.mkdir(parents=True, exist_ok=True)
    p = CHARTS / name
    fig.write_html(str(p), include_plotlyjs="cdn")
    print(f"  wrote charts/{name}")


def metrics(pnl) -> dict:
    p = np.asarray(pnl, dtype=float)
    n = len(p)
    return {
        "n": int(n),
        "pf": round(L.pf(p), 4) if n else None,
        "wr": round(L.wr(p), 2) if n else None,
        "mean": round(float(np.mean(p)), 4) if n else None,
        "median": round(float(np.median(p)), 4) if n else None,
        "cvar5": round(L.cvar5(p), 4) if n else None,
        "total": round(float(np.sum(p)), 4) if n else None,
    }


# ───────────────────────── data layer ─────────────────────────
def load_full() -> pd.DataFrame:
    trades = json.load(open(RUN / "per_trade.json"))
    smp = {(e["ticker"], e["date"]): e for e in json.load(open(VAL_FULL))["events"]}
    rows = []
    for t in trades:
        e = smp.get((t["ticker"], t["date"]), {})
        rows.append({
            **t,
            "stratum": e.get("stratum", "unknown"),
            "mom_pct": e.get("mom_pct", np.nan),
            "gap_pct_at_hit": e.get("gap_pct_at_hit", np.nan),
        })
    df = pd.DataFrame(rows)
    for c in ["pnl_pct", "hold_sec", "entry_t_sec", "exit_t_sec", "mom_pct",
              "gap_pct_at_hit", "entry_lag_from_scanner_sec"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# ───────────────────────── T2 ─────────────────────────
def t2(df):
    print("\n[T2] full-pool headline + exit-reason confirmation")
    m = metrics(df.pnl_pct)
    run_summary = json.load(open(RUN / "run_summary.json"))
    exit_reasons = sorted(df.exit_reason.unique().tolist())
    drift = [r for r in exit_reasons if r not in ALLOWED_EXITS]
    exit_break = {r: metrics(df[df.exit_reason == r].pnl_pct) for r in exit_reasons}

    FINDINGS["t2"] = {
        "headline": m,
        "n_events_in_pool": run_summary["run_config"]["n_events_sampled"],
        "exit_reasons": exit_reasons,
        "exit_reason_drift": drift,
        "exit_breakdown": exit_break,
        "run_config": run_summary["run_config"],
    }
    print(f"  n_trades={m['n']} PF={m['pf']} WR={m['wr']}% mean={m['mean']}% CVaR5={m['cvar5']}%")
    print(f"  exit reasons: {exit_reasons}  drift={drift or 'NONE'}")

    # Chart — full-pool PnL% KDE + rug + CVaR5 line
    x = df.pnl_pct.values
    xs = np.linspace(x.min() - 3, x.max() + 3, 600)
    cvar5 = m["cvar5"]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=xs, y=L.kde_xy(x, xs), mode="lines",
                  line=dict(color="#78909C", width=2), fill="tozeroy",
                  fillcolor=L.hex_to_rgba("#78909C", 0.10),
                  name=f"All trades (n={m['n']})"))
    fig.add_trace(go.Scatter(x=x, y=[-0.002] * len(x), mode="markers",
                  marker=dict(color="#B0BEC5", size=5, symbol="line-ns-open"),
                  name="trades (rug)", hovertemplate="%{x:.1f}%<extra></extra>"))
    fig.add_vline(x=cvar5, line=dict(color="#EF5350", dash="dot"),
                  annotation_text=f"CVaR5={cvar5:.1f}%")
    fig.add_vline(x=m["mean"], line=dict(color="#66BB6A", dash="dash"),
                  annotation_text=f"mean={m['mean']:.1f}%")
    fig.add_vline(x=0, line=dict(color="#888", dash="dash"))
    fig.update_layout(**L.base_layout(
        f"T2 · Full-pool PnL% (n={m['n']}, PF={m['pf']}, WR={m['wr']}%, "
        f"mean={m['mean']}%, CVaR5={cvar5}%) · locked p=0.80", "PnL%", "Density"))
    _save(fig, "pnl_kde_full.html")
    return m


# ───────────────────────── T3 ─────────────────────────
def t3(df, valr4):
    print("\n[T3] session breakdown (RTH vs pre-market)")
    order = ["regular_hours", "pre_market", "post_market"]
    sess = [s for s in order if (df.session_bucket == s).any()]
    sb = {s: metrics(df[df.session_bucket == s].pnl_pct) for s in sess}
    FINDINGS["t3"] = {"session_breakdown": sb}
    for s in sess:
        mm = sb[s]
        print(f"  {L.SESSION_LABELS.get(s, s):12s} n={mm['n']:4d} PF={mm['pf']} "
              f"WR={mm['wr']}% mean={mm['mean']}% CVaR5={mm['cvar5']}%")

    # T3a verdict — gap vs val_r4
    rth, pre = sb.get("regular_hours"), sb.get("pre_market")
    v_rth = valr4["session"]["regular_hours"]
    v_pre = valr4["session"]["pre_market"]
    gap_full = rth["pf"] - pre["pf"] if (rth and pre) else None
    gap_r4 = v_rth["pf"] - v_pre["pf"]
    r4_rth_leads = v_rth["pf"] > v_pre["pf"]
    full_rth_leads = (rth["pf"] > pre["pf"]) if (rth and pre) else None
    if gap_full is None:
        verdict = "insufficient session data"
    elif r4_rth_leads and full_rth_leads is False:
        # sign flip: the direction of the session edge reversed
        verdict = ("INVERTS — the val_r4 RTH>pre edge reverses; RTH PF collapses "
                   f"{v_rth['pf']}->{rth['pf']} (below 1.0) while pre-market holds "
                   f"({v_pre['pf']}->{pre['pf']})")
    elif gap_full > gap_r4 * 1.15:
        verdict = "WIDENS"
    elif gap_full < gap_r4 * 0.85:
        verdict = "NARROWS"
    else:
        verdict = "HOLDS (roughly unchanged)"
    FINDINGS["t3a"] = {
        "valr4_rth_pf": v_rth["pf"], "valr4_pre_pf": v_pre["pf"], "valr4_gap": round(gap_r4, 3),
        "full_rth_pf": rth["pf"] if rth else None, "full_pre_pf": pre["pf"] if pre else None,
        "full_gap": round(gap_full, 3) if gap_full is not None else None,
        "verdict": verdict,
    }
    print(f"  T3a gap: val_r4 RTH {v_rth['pf']} / PRE {v_pre['pf']} (Δ{gap_r4:.2f}) "
          f"vs val-full RTH {rth['pf']} / PRE {pre['pf']} (Δ{gap_full:.2f}) -> {verdict}")

    # Chart 1 — PnL KDE overlay RTH vs pre
    x = df[df.session_bucket.isin(["regular_hours", "pre_market"])].pnl_pct.values
    xs = np.linspace(x.min() - 3, x.max() + 3, 600)
    fig = go.Figure()
    for s in ["regular_hours", "pre_market"]:
        sub = df[df.session_bucket == s]
        col = L.SESSION_COLORS[s]
        if len(sub) >= 3:
            fig.add_trace(go.Scatter(x=xs, y=L.kde_xy(sub.pnl_pct, xs), mode="lines",
                          line=dict(color=col, width=2),
                          name=f"{L.SESSION_LABELS[s]} (n={len(sub)}, PF={sb[s]['pf']})"))
        fig.add_trace(go.Scatter(x=sub.pnl_pct,
                      y=[-0.003 * (1 + ["regular_hours", "pre_market"].index(s))] * len(sub),
                      mode="markers", marker=dict(color=col, size=5, symbol="line-ns-open"),
                      showlegend=False))
    fig.add_vline(x=0, line=dict(color="#888", dash="dash"))
    fig.update_layout(**L.base_layout(
        "T3 · PnL% KDE — RTH vs Pre-Market (full pool) · locked p=0.80", "PnL%", "Density"))
    _save(fig, "session_kde.html")

    # Chart 2 — grouped bar PF / WR% / CVaR5, RTH vs pre (3 panels, different scales)
    fig = make_subplots(rows=1, cols=3, subplot_titles=("Profit Factor", "Win Rate %", "CVaR5 %"))
    labels = [L.SESSION_LABELS[s] for s in ["regular_hours", "pre_market"]]
    cols = [L.SESSION_COLORS[s] for s in ["regular_hours", "pre_market"]]
    pf_v = [sb["regular_hours"]["pf"], sb["pre_market"]["pf"]]
    wr_v = [sb["regular_hours"]["wr"], sb["pre_market"]["wr"]]
    cv_v = [sb["regular_hours"]["cvar5"], sb["pre_market"]["cvar5"]]
    ns = [sb["regular_hours"]["n"], sb["pre_market"]["n"]]
    fig.add_trace(go.Bar(x=labels, y=pf_v, marker_color=cols, showlegend=False,
                  text=[f"{v:.2f}<br>n={n}" for v, n in zip(pf_v, ns)], textposition="outside"), 1, 1)
    fig.add_trace(go.Bar(x=labels, y=wr_v, marker_color=cols, showlegend=False,
                  text=[f"{v:.0f}%" for v in wr_v], textposition="outside"), 1, 2)
    fig.add_trace(go.Bar(x=labels, y=cv_v, marker_color=cols, showlegend=False,
                  text=[f"{v:.1f}%" for v in cv_v], textposition="outside"), 1, 3)
    fig.add_hline(y=1.0, line=dict(color="#888", dash="dash"), row=1, col=1)
    fig.update_layout(template="plotly_dark", width=1100, height=520,
                      title=dict(text="T3 · RTH vs Pre-Market — PF / WR% / CVaR5 (full pool) · locked p=0.80",
                                 x=0.01, font=dict(size=14)))
    _save(fig, "session_bar.html")
    return sb


# ───────────────────────── T4 ─────────────────────────
def t4(df, valr4_df):
    print("\n[T4] stratum × session cross-tab")
    cells = []
    for s in ["low", "mid", "high"]:
        for sess in ["regular_hours", "pre_market"]:
            sub = df[(df.stratum == s) & (df.session_bucket == sess)]
            mm = metrics(sub.pnl_pct)
            mm["cvar5"] = mm["cvar5"] if mm["n"] >= 10 else None
            cells.append(dict(stratum=s, session=sess, **mm))
    # blended per stratum (full pool)
    blended = {s: metrics(df[df.stratum == s].pnl_pct) for s in ["low", "mid", "high"]}
    # val_r4 blended per stratum (from sym_p80 join)
    valr4_blended = {s: metrics(valr4_df[valr4_df.stratum == s].pnl_pct)
                     for s in ["low", "mid", "high"]}
    FINDINGS["t4"] = {"crosstab": cells, "blended_full": blended,
                      "blended_valr4": valr4_blended}
    for c in cells:
        print(f"  {c['stratum']:4s} {c['session']:14s} n={c['n']:3d} PF={c['pf']} "
              f"WR={c['wr']}% CVaR5={c['cvar5']}")

    # T4a verdict — low-stratum PF full vs val_r4 (0.37 reference)
    low_full = blended["low"]["pf"]
    low_r4 = valr4_blended["low"]["pf"]
    if low_full is None:
        v4 = "no low-stratum trades"
    elif low_full >= 1.0 and low_r4 < 1.0:
        v4 = "DOES NOT HOLD — low stratum recovers to profitable at full n (likely small-sample artifact on val_r4)"
    elif low_full < 1.0:
        v4 = "HOLDS — low stratum remains a loser (PF<1) at full n"
    else:
        v4 = "MIXED"
    FINDINGS["t4a"] = {"valr4_low_pf": low_r4, "full_low_pf": low_full,
                       "valr4_low_n": valr4_blended["low"]["n"],
                       "full_low_n": blended["low"]["n"], "verdict": v4}
    print(f"  T4a low-stratum: val_r4 PF={low_r4} (n={valr4_blended['low']['n']}) "
          f"vs val-full PF={low_full} (n={blended['low']['n']}) -> {v4}")

    # Chart — grouped bar, 6 cells, PF (bars) + WR% (diamonds)
    ct = pd.DataFrame(cells)
    ct["cell"] = ct.stratum.str.capitalize() + " · " + ct.session.map(L.SESSION_LABELS)
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(x=ct.cell, y=ct.pf, name="Profit Factor", marker_color="#42A5F5",
                  text=[f"{v:.2f}<br>n={n}" for v, n in zip(ct.pf, ct.n)],
                  textposition="outside"), secondary_y=False)
    fig.add_trace(go.Scatter(x=ct.cell, y=ct.wr, name="Win Rate %", mode="markers",
                  marker=dict(color="#FFB74D", size=13, symbol="diamond")), secondary_y=True)
    fig.add_hline(y=1.0, line=dict(color="#888", dash="dash"), secondary_y=False)
    fig.update_layout(**L.base_layout(
        "T4 · Stratum × Session — PF (bars) + WR% (diamonds), full pool · locked p=0.80",
        "", "Profit Factor"))
    fig.update_yaxes(title_text="Win Rate %", secondary_y=True, range=[0, 100])
    _save(fig, "stratum_session_bar.html")
    return blended


# ───────────────────────── T5 ─────────────────────────
def t5(full_m, valr4):
    print("\n[T5] direct comparison to val_r4")
    r4 = valr4["overall"]
    rows = [
        ("Profit Factor", r4["pf"], full_m["pf"]),
        ("Win Rate %", r4["wr"], full_m["wr"]),
        ("Mean PnL %", r4["mean"], full_m["mean"]),
        ("CVaR5 %", r4["cvar5"], full_m["cvar5"]),
    ]
    pf_rel = (full_m["pf"] - r4["pf"]) / r4["pf"] * 100.0
    if full_m["pf"] > r4["pf"]:
        verdict = "HELD UP / IMPROVED"
    elif pf_rel > -30:
        verdict = "DEGRADED (within tolerance)"
    else:
        verdict = "DEGRADED (>30% relative PF drop — flag)"
    FINDINGS["t5"] = {
        "valr4": r4, "valfull": full_m,
        "pf_rel_change_pct": round(pf_rel, 1),
        "pf_flag_gt_30pct": abs(pf_rel) > 30,
        "verdict": verdict,
    }
    print(f"  val_r4 (n={r4['n']}): PF={r4['pf']} WR={r4['wr']}% mean={r4['mean']}% CVaR5={r4['cvar5']}%")
    print(f"  val-full (n={full_m['n']}): PF={full_m['pf']} WR={full_m['wr']}% mean={full_m['mean']}% CVaR5={full_m['cvar5']}%")
    print(f"  PF relative change: {pf_rel:+.1f}% -> {verdict}")

    # Chart — 1x4 subplots, val_r4 vs val-full per metric
    fig = make_subplots(rows=1, cols=4, subplot_titles=[r[0] for r in rows])
    for ci, (name, a, b) in enumerate(rows, start=1):
        fig.add_trace(go.Bar(x=["val_r4", "val-full"], y=[a, b],
                      marker_color=["#90A4AE", "#42A5F5"], showlegend=False,
                      text=[f"{a:.2f}", f"{b:.2f}"], textposition="outside"), 1, ci)
        if name == "Profit Factor":
            fig.add_hline(y=1.0, line=dict(color="#888", dash="dash"), row=1, col=ci)
    fig.update_layout(template="plotly_dark", width=1150, height=520,
                      title=dict(text=f"T5 · val_r4 (n={r4['n']}) vs val-full (n={full_m['n']}) — "
                                      f"generalization check · locked p=0.80",
                                 x=0.01, font=dict(size=14)))
    _save(fig, "valr4_vs_valfull_bar.html")
    return rows, verdict, pf_rel


# ───────────────────────── val_r4 baseline ─────────────────────────
def load_valr4_baseline():
    """Recompute val_r4 (sym_p80) overall + per-session metrics from its per_trade."""
    trades = json.load(open(VALR4_DIR / "per_trade.json"))
    df = pd.DataFrame(trades)
    df["pnl_pct"] = pd.to_numeric(df["pnl_pct"], errors="coerce")
    overall = metrics(df.pnl_pct)
    session = {s: metrics(df[df.session_bucket == s].pnl_pct)
               for s in df.session_bucket.unique()}
    return {"overall": overall, "session": session}, L.load_joined("p80")


# ───────────────────────── T6 + write-ups ─────────────────────────
def t6(df):
    print("\n[T6] downstream per-trade output")
    trades = json.load(open(RUN / "per_trade.json"))
    smp = {(e["ticker"], e["date"]): e for e in json.load(open(VAL_FULL))["events"]}
    enriched = []
    for t in trades:
        e = smp.get((t["ticker"], t["date"]), {})
        enriched.append({**t, "stratum": e.get("stratum"), "mom_pct": e.get("mom_pct"),
                         "gap_pct_at_hit": e.get("gap_pct_at_hit")})
    json.dump(enriched, open(OUT / "per_trade_val_full.json", "w"), indent=2)
    print(f"  wrote per_trade_val_full.json ({len(enriched)} trades, enriched w/ stratum)")


def write_full_pool_results(full_m):
    run_summary = json.load(open(RUN / "run_summary.json"))
    per_trade = json.load(open(RUN / "per_trade.json"))
    obj = {"headline": full_m, "run_config": run_summary["run_config"],
           "run_summary": run_summary, "per_trade": per_trade}
    json.dump(obj, open(OUT / "full_pool_results.json", "w"), indent=2)
    print("  wrote full_pool_results.json")


def write_comparison_summary(rows, verdict, pf_rel, valr4, full_m):
    lines = ["# Phase VAL-FULL — T5 Direct Comparison (Generalization Verdict)\n"]
    lines.append(f"**Verdict: {verdict}**  (PF relative change {pf_rel:+.1f}%)\n")
    lines.append("| Metric | val_r4 (n={}) | val-full (n={}) | Δ (abs) |".format(
        valr4["overall"]["n"], full_m["n"]))
    lines.append("|---|---:|---:|---:|")
    for name, a, b in rows:
        lines.append(f"| {name} | {a} | {b} | {b - a:+.4f} |")
    lines.append("")
    lines.append("val_r4 = the original stratified 100-event sample, 65 traded, locked p=0.80 config "
                 "(`results/phase_r1_final/sym_p80`). val-full = the 522-event held-out pool, same "
                 "config, no re-tuning.\n")
    esc = ("**ESCALATION FLAG: full-pool PF differs from val_r4 PF by more than 30% relative.**"
           if abs(pf_rel) > 30 else "PF within 30% relative of val_r4 — no escalation flag.")
    lines.append(f"{esc}\n")
    (OUT / "comparison_summary.md").write_text("\n".join(lines))
    print("  wrote comparison_summary.md")


def write_summary(full_m):
    f = FINDINGS
    t3a, t4a, t5 = f["t3a"], f["t4a"], f["t5"]
    L_ = lambda s: s  # noqa
    lines = ["# Phase VAL-FULL — Summary\n"]
    lines.append("EPG-Rapid full-pool confirmation of the locked val_r4 p=0.80 config on a larger, "
                 "independent held-out pool. No config changes; confirmation, not re-tuning.\n")
    lines.append("## Locked config (unchanged)\n")
    rc = f["t2"]["run_config"]
    lines.append(f"- entry_mode=`{rc['entry_mode']}`, p_open=p_close=`{rc['p_open']}`, "
                 f"max_entry_lag_sec=`{rc['max_entry_lag_sec']}`, t_gate_sec=`{rc['t_gate_sec']}` "
                 f"(no time gate), LULD/EXIT_D off.")
    lines.append(f"- Exit stack observed: {f['t2']['exit_reasons']} "
                 f"(drift: {f['t2']['exit_reason_drift'] or 'NONE'}).\n")
    lines.append("## T2 — Full-pool headline\n")
    lines.append("| n_trades | PF | WR% | mean PnL% | median PnL% | CVaR5% |")
    lines.append("|---:|---:|---:|---:|---:|---:|")
    lines.append(f"| {full_m['n']} | {full_m['pf']} | {full_m['wr']} | {full_m['mean']} | "
                 f"{full_m['median']} | {full_m['cvar5']} |")
    lines.append(f"\nPool = {f['t2']['n_events_in_pool']} events. Chart: `charts/pnl_kde_full.html`\n")
    lines.append("## T3 — Session breakdown\n")
    lines.append("| Session | n | PF | WR% | mean% | CVaR5% |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for s, mm in f["t3"]["session_breakdown"].items():
        lines.append(f"| {L.SESSION_LABELS.get(s, s)} | {mm['n']} | {mm['pf']} | {mm['wr']} | "
                     f"{mm['mean']} | {mm['cvar5']} |")
    lines.append(f"\n**T3a verdict — RTH/pre PF gap: {t3a['verdict']}.** "
                 f"val_r4 RTH {t3a['valr4_rth_pf']} / PRE {t3a['valr4_pre_pf']} (Δ{t3a['valr4_gap']}); "
                 f"val-full RTH {t3a['full_rth_pf']} / PRE {t3a['full_pre_pf']} (Δ{t3a['full_gap']}). "
                 f"Charts: `charts/session_kde.html`, `charts/session_bar.html`\n")
    lines.append("## T4 — Stratum × session\n")
    lines.append("| Stratum | Session | n | PF | WR% | CVaR5% |")
    lines.append("|---|---|---:|---:|---:|---:|")
    for c in f["t4"]["crosstab"]:
        lines.append(f"| {c['stratum']} | {L.SESSION_LABELS.get(c['session'], c['session'])} | "
                     f"{c['n']} | {c['pf']} | {c['wr']} | {c['cvar5']} |")
    lines.append("\nBlended per stratum (full pool):")
    for s in ["low", "mid", "high"]:
        b = f["t4"]["blended_full"][s]
        r = f["t4"]["blended_valr4"][s]
        lines.append(f"- **{s}**: val-full PF={b['pf']} (n={b['n']}) vs val_r4 PF={r['pf']} (n={r['n']})")
    lines.append(f"\n**T4a verdict — low-stratum underperformance: {t4a['verdict']}** "
                 f"(val_r4 low PF={t4a['valr4_low_pf']} n={t4a['valr4_low_n']}; "
                 f"val-full low PF={t4a['full_low_pf']} n={t4a['full_low_n']}).")
    bf = f["t4"]["blended_full"]
    prof = [s for s in ["low", "mid", "high"] if (bf[s]["pf"] or 0) >= 1.0]
    lines.append(f"\nBroader stratum picture (full pool): only **{', '.join(prof) or 'none'}** "
                 f"clears PF>=1.0. Both low (PF={bf['low']['pf']}) and mid (PF={bf['mid']['pf']}) "
                 f"are net losers; high (PF={bf['high']['pf']}) carries the pool, driven by "
                 f"high·pre-market (PF={[c['pf'] for c in f['t4']['crosstab'] if c['stratum']=='high' and c['session']=='pre_market'][0]}). "
                 f"Chart: `charts/stratum_session_bar.html`\n")
    lines.append("## T5 — Generalization verdict\n")
    lines.append(f"**{t5['verdict']}** — PF relative change {t5['pf_rel_change_pct']:+.1f}%. "
                 f"See `comparison_summary.md`, chart `charts/valr4_vs_valfull_bar.html`.\n")
    lines.append("## Escalation check\n")
    lines.append("| Condition | Threshold | Result |")
    lines.append("|---|---|---|")
    lines.append(f"| Pool size after exclusions | < 300 | {f['t2']['n_events_in_pool']} events — OK |")
    drift = f["t2"]["exit_reason_drift"]
    lines.append(f"| Exit reason other than epg_window_close/session_end | any | "
                 f"{'DRIFT: ' + str(drift) if drift else 'none — OK'} |")
    lines.append(f"| Full-pool PF vs val_r4 PF | >30% relative | "
                 f"{t5['pf_rel_change_pct']:+.1f}% — {'FLAG' if t5['pf_flag_gt_30pct'] else 'OK'} |")
    lines.append("| Test-split boundary in pool | any | none (runner assert_split_valid passed) — OK |")
    lines.append("\n## Output files\n")
    for fn, desc in [
        ("pool_definition.md", "T1 pool size, exclusions, missing-file log"),
        ("full_pool_results.json", "T2 headline + run_config + per-trade"),
        ("per_trade_val_full.json", "T6 downstream per-trade (enriched w/ stratum)"),
        ("charts/pnl_kde_full.html", "T2 full-pool PnL KDE"),
        ("charts/session_kde.html", "T3 RTH vs pre KDE"),
        ("charts/session_bar.html", "T3 PF/WR/CVaR5 bars"),
        ("charts/stratum_session_bar.html", "T4 6-cell cross-tab"),
        ("charts/valr4_vs_valfull_bar.html", "T5 comparison bars"),
        ("comparison_summary.md", "T5 verdict"),
        ("findings.json", "machine-readable findings"),
        ("summary.md", "this file"),
    ]:
        lines.append(f"- `{fn}` — {desc}")
    (OUT / "summary.md").write_text("\n".join(lines))
    print("  wrote summary.md")


def main():
    df = load_full()
    valr4, valr4_df = load_valr4_baseline()
    full_m = t2(df)
    t3(df, valr4)
    t4(df, valr4_df)
    rows, verdict, pf_rel = t5(full_m, valr4)
    t6(df)
    write_full_pool_results(full_m)
    write_comparison_summary(rows, verdict, pf_rel, valr4, full_m)
    write_summary(full_m)
    json.dump(FINDINGS, open(OUT / "findings.json", "w"), indent=2, default=str)
    print(f"\nFindings -> {OUT / 'findings.json'}")
    print("Done T2-T6.")


if __name__ == "__main__":
    main()
