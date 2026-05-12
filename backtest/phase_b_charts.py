#!/usr/bin/env python3
"""Phase B — Summary chart builder (T7).

Generates 5 standalone Plotly HTML charts from Phase B 100-event val results.
Reads from results/phase_b/100_val_seed42/ by default.

Usage:
    python -m backtest.phase_b_charts
    python -m backtest.phase_b_charts --results-dir results/phase_b/100_val_seed42
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ── Reference values from prior phases ─────────────────────────────────────
# Phase A: EPG+LULD, no EXIT_D (this run, 100-event val seed=42)
_PHASE_A_PF = 1.0387
_PHASE_A_RTH_PF = 1.0753
_PHASE_A_PM_PF = 1.0088

# T10 best (theta=0.65, tau=4s) — on 100-event val seed=42
_T10_PF = 1.3848

# Phase A exit breakdown (for comparison)
_PHASE_A_EXIT = {
    "epg_window_close": {"n": 351, "pf": 1.4751},
    "luld_proximity": {"n": 34, "pf": 0.1413},
}


_COLORS = {
    "phase_a": "#6c757d",
    "t10_ref": "#fd7e14",
    "phase_b": "#0d6efd",
    "first": "#198754",
    "reentry": "#dc3545",
    "rth": "#0d6efd",
    "pre_market": "#6f42c1",
}

_PLOTLY_CONFIG = {"displayModeBar": True, "responsive": True}


def _pf_from_trades(df: pd.DataFrame) -> float:
    wins = df.loc[df["pnl_pct"] > 0, "pnl_pct"].sum()
    losses = abs(df.loc[df["pnl_pct"] < 0, "pnl_pct"].sum())
    return wins / losses if losses > 0 else float("nan")


def _metrics(df: pd.DataFrame) -> dict:
    pf = _pf_from_trades(df)
    win_rate = (df["pnl_pct"] > 0).mean() if len(df) > 0 else float("nan")
    mean_pnl = df["pnl_pct"].mean() if len(df) > 0 else float("nan")
    return {"pf": pf, "n": len(df), "win_rate": win_rate, "mean_pnl": mean_pnl}


# ── Chart 01: PF Comparison ─────────────────────────────────────────────────

def chart_01_pf_comparison(df: pd.DataFrame, out_dir: Path) -> None:
    first_df = df[df["entry_type"] == "first"]
    reentry_df = df[df["entry_type"] == "reentry"]

    phase_b_pf = _pf_from_trades(df)
    first_pf = _pf_from_trades(first_df)
    reentry_pf = _pf_from_trades(reentry_df)

    labels = [
        "Phase A<br>(EPG+LULD,<br>no EXIT_D)",
        "T10 ref<br>(θ=0.65 τ=4s,<br>no reentry)",
        "Phase B<br>Overall",
        "Phase B<br>First entries",
        "Phase B<br>Re-entries",
    ]
    values = [_PHASE_A_PF, _T10_PF, phase_b_pf, first_pf, reentry_pf]
    colors = [
        _COLORS["phase_a"],
        _COLORS["t10_ref"],
        _COLORS["phase_b"],
        _COLORS["first"],
        _COLORS["reentry"],
    ]

    fig = go.Figure()
    for label, val, color in zip(labels, values, colors):
        fig.add_bar(
            x=[label],
            y=[val],
            marker_color=color,
            text=[f"{val:.4f}"],
            textposition="outside",
            name=label.replace("<br>", " "),
        )

    fig.add_hline(y=1.0, line_dash="dash", line_color="gray", opacity=0.6,
                  annotation_text="Break-even PF=1.0", annotation_position="bottom right")
    fig.add_hline(y=_T10_PF, line_dash="dot", line_color=_COLORS["t10_ref"], opacity=0.5,
                  annotation_text=f"T10 ref {_T10_PF:.4f}", annotation_position="top left")

    fig.update_layout(
        title="Phase B — Profit Factor Comparison",
        yaxis_title="Profit Factor",
        showlegend=False,
        yaxis=dict(range=[0, max(values) * 1.2]),
        template="plotly_white",
        height=480,
    )
    fig.write_html(str(out_dir / "01_pf_comparison.html"), config=_PLOTLY_CONFIG)
    print(f"  wrote 01_pf_comparison.html  (Phase B PF={phase_b_pf:.4f})")


# ── Chart 02: Exit Breakdown ────────────────────────────────────────────────

def chart_02_exit_breakdown(df: pd.DataFrame, out_dir: Path) -> None:
    reasons = df["exit_reason"].value_counts()
    total = len(df)

    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=["Trade Count by Exit Reason", "Profit Factor by Exit Reason"],
        horizontal_spacing=0.12,
    )

    reason_colors = {
        "exit_d": "#fd7e14",
        "luld_proximity": "#dc3545",
        "epg_window_close": "#198754",
        "session_end": "#6c757d",
    }

    reason_order = ["exit_d", "luld_proximity", "epg_window_close", "session_end"]
    present = [r for r in reason_order if r in reasons.index]

    counts = [int(reasons.get(r, 0)) for r in present]
    pcts = [c / total * 100 for c in counts]
    pfs = [_pf_from_trades(df[df["exit_reason"] == r]) for r in present]
    clrs = [reason_colors.get(r, "#adb5bd") for r in present]

    labels = [r.replace("_", "<br>") for r in present]

    fig.add_bar(
        x=labels, y=counts,
        marker_color=clrs,
        text=[f"{c}<br>({p:.1f}%)" for c, p in zip(counts, pcts)],
        textposition="outside",
        row=1, col=1,
    )
    fig.add_bar(
        x=labels, y=pfs,
        marker_color=clrs,
        text=[f"{p:.3f}" if not np.isnan(p) else "N/A" for p in pfs],
        textposition="outside",
        row=1, col=2,
    )
    fig.add_hline(y=1.0, line_dash="dash", line_color="gray", opacity=0.5, row=1, col=2)

    fig.update_layout(
        title="Phase B — Exit Reason Breakdown",
        showlegend=False,
        template="plotly_white",
        height=460,
    )
    fig.update_yaxes(title_text="Trade count", row=1, col=1)
    fig.update_yaxes(title_text="Profit Factor", row=1, col=2)
    fig.write_html(str(out_dir / "02_exit_breakdown.html"), config=_PLOTLY_CONFIG)
    print(f"  wrote 02_exit_breakdown.html  (exit reasons: {dict(reasons)})")


# ── Chart 03: Re-entry Count Distribution ──────────────────────────────────

def chart_03_reentry_distribution(per_event_path: Path, out_dir: Path) -> None:
    with open(per_event_path) as f:
        events = json.load(f)

    counts = [e.get("n_reentries_in_event", 0) for e in events]
    max_count = max(counts) if counts else 0
    bins = list(range(0, max_count + 2))
    hist_counts = [counts.count(b) for b in bins[:-1]]

    fig = go.Figure()
    fig.add_bar(
        x=[str(b) for b in bins[:-1]],
        y=hist_counts,
        marker_color=_COLORS["reentry"],
        text=hist_counts,
        textposition="outside",
    )

    n_events = len(counts)
    n_with = sum(1 for c in counts if c > 0)
    mean_c = np.mean(counts)

    fig.update_layout(
        title=(
            f"Phase B — Re-entry Count Distribution per Event<br>"
            f"<sub>{n_events} events | {n_with} with ≥1 re-entry | "
            f"mean {mean_c:.2f} re-entries/event</sub>"
        ),
        xaxis_title="Re-entries in event",
        yaxis_title="Number of events",
        template="plotly_white",
        height=440,
        showlegend=False,
    )
    fig.write_html(str(out_dir / "03_reentry_count_distribution.html"), config=_PLOTLY_CONFIG)
    print(f"  wrote 03_reentry_count_distribution.html  (mean={mean_c:.2f}, max={max_count})")


# ── Chart 04: Session Breakdown ─────────────────────────────────────────────

def chart_04_session_breakdown(df: pd.DataFrame, out_dir: Path) -> None:
    sessions = df["session_bucket"].value_counts()
    session_order = ["regular_hours", "pre_market", "after_hours"]
    present_sessions = [s for s in session_order if s in sessions.index]

    phase_b_pfs = {}
    phase_b_ns = {}
    for s in present_sessions:
        sub = df[df["session_bucket"] == s]
        phase_b_pfs[s] = _pf_from_trades(sub)
        phase_b_ns[s] = len(sub)

    # Build grouped bar: Phase A reference vs Phase B
    session_labels = {
        "regular_hours": "RTH",
        "pre_market": "Pre-market",
        "after_hours": "After-hours",
    }

    phase_a_ref = {
        "regular_hours": _PHASE_A_RTH_PF,
        "pre_market": _PHASE_A_PM_PF,
        "after_hours": float("nan"),
    }

    fig = go.Figure()

    # Phase A bars
    fig.add_bar(
        name="Phase A (baseline)",
        x=[session_labels[s] for s in present_sessions],
        y=[phase_a_ref.get(s, float("nan")) for s in present_sessions],
        marker_color=_COLORS["phase_a"],
        text=[f"{phase_a_ref.get(s, float('nan')):.4f}" for s in present_sessions],
        textposition="outside",
    )

    # Phase B bars
    fig.add_bar(
        name="Phase B",
        x=[session_labels[s] for s in present_sessions],
        y=[phase_b_pfs[s] for s in present_sessions],
        marker_color=_COLORS["phase_b"],
        text=[
            f"{phase_b_pfs[s]:.4f}<br>(n={phase_b_ns[s]})"
            for s in present_sessions
        ],
        textposition="outside",
    )

    fig.add_hline(y=1.0, line_dash="dash", line_color="gray", opacity=0.5,
                  annotation_text="Break-even", annotation_position="bottom right")

    fig.update_layout(
        title="Phase B — Session Breakdown vs Phase A",
        yaxis_title="Profit Factor",
        barmode="group",
        template="plotly_white",
        height=480,
        legend=dict(orientation="h", y=-0.15),
    )
    fig.write_html(str(out_dir / "04_session_breakdown.html"), config=_PLOTLY_CONFIG)
    print(f"  wrote 04_session_breakdown.html  (sessions: {list(sessions.index)})")


# ── Chart 05: Equity Curve ──────────────────────────────────────────────────

def chart_05_equity_curve(df: pd.DataFrame, out_dir: Path) -> None:
    # Sort by entry timestamp for chronological curve
    sort_col = "entry_ts" if "entry_ts" in df.columns else df.columns[0]
    df_sorted = df.sort_values(sort_col).reset_index(drop=True)
    df_sorted["trade_num"] = range(1, len(df_sorted) + 1)
    df_sorted["cum_pnl"] = df_sorted["pnl_pct"].cumsum()

    first_df = df_sorted[df_sorted["entry_type"] == "first"].copy()
    reentry_df = df_sorted[df_sorted["entry_type"] == "reentry"].copy()
    first_df["cum_pnl_et"] = first_df["pnl_pct"].cumsum()
    reentry_df["cum_pnl_et"] = reentry_df["pnl_pct"].cumsum()

    fig = go.Figure()

    fig.add_scatter(
        x=df_sorted["trade_num"],
        y=df_sorted["cum_pnl"],
        name="All trades",
        line=dict(color=_COLORS["phase_b"], width=2),
        mode="lines",
    )
    fig.add_scatter(
        x=first_df["trade_num"],
        y=first_df["cum_pnl_et"],
        name="First entries only",
        line=dict(color=_COLORS["first"], width=1.5, dash="dot"),
        mode="lines",
    )
    fig.add_scatter(
        x=reentry_df["trade_num"],
        y=reentry_df["cum_pnl_et"],
        name="Re-entries only",
        line=dict(color=_COLORS["reentry"], width=1.5, dash="dash"),
        mode="lines",
    )

    n_first = len(first_df)
    n_reentry = len(reentry_df)
    total_pnl = df_sorted["cum_pnl"].iloc[-1]

    fig.update_layout(
        title=(
            f"Phase B — Cumulative PnL (trade sequence order)<br>"
            f"<sub>{len(df_sorted)} trades: {n_first} first + {n_reentry} reentry | "
            f"total PnL {total_pnl:+.2f}%</sub>"
        ),
        xaxis_title="Trade number",
        yaxis_title="Cumulative PnL (%)",
        template="plotly_white",
        height=500,
        legend=dict(orientation="h", y=-0.15),
    )
    fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.4)
    fig.write_html(str(out_dir / "05_equity_curve.html"), config=_PLOTLY_CONFIG)
    print(f"  wrote 05_equity_curve.html  (total PnL={total_pnl:+.2f}%)")


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Phase B summary charts (T7)")
    parser.add_argument(
        "--results-dir",
        default="results/phase_b/100_val_seed42",
        help="Path to Phase B results directory",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory for charts (default: results-dir/charts)",
    )
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    out_dir = Path(args.output_dir) if args.output_dir else results_dir / "charts"
    out_dir.mkdir(parents=True, exist_ok=True)

    per_trade_path = results_dir / "per_trade.parquet"
    per_event_path = results_dir / "per_event_summary.json"

    if not per_trade_path.exists():
        raise FileNotFoundError(f"per_trade.parquet not found: {per_trade_path}")
    if not per_event_path.exists():
        raise FileNotFoundError(f"per_event_summary.json not found: {per_event_path}")

    df = pd.read_parquet(per_trade_path)
    print(f"Loaded {len(df)} trades from {per_trade_path}")

    if "entry_type" not in df.columns:
        raise ValueError("per_trade.parquet missing 'entry_type' column — run with Phase B config")

    print(f"Generating 5 charts -> {out_dir}/")
    chart_01_pf_comparison(df, out_dir)
    chart_02_exit_breakdown(df, out_dir)
    chart_03_reentry_distribution(per_event_path, out_dir)
    chart_04_session_breakdown(df, out_dir)
    chart_05_equity_curve(df, out_dir)
    print("Done.")


if __name__ == "__main__":
    main()
