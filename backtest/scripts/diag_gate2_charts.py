#!/usr/bin/env python3
"""
Phase DIAG-GATE-2: Annotated entry/exit charts from cached DIAG-GATE replay data.

T1: Derive 6 annotation timestamps per event; write annotation_summary JSON.
T2: 2-panel Plotly HTML per event (OHLC + arrows, ratio + p_close).
T3: Write index2.html.

No backtest re-run. Reads from:
  results/phase_diag_gate/replay_data/{TICKER}_{DATE}.json
  results/phase_diag_gate/ohlc_30s/{TICKER}_{DATE}.json
  results/phase_diag_gate/selected_events.json
"""

from __future__ import annotations
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DIAG_DIR  = PROJECT_ROOT / "backtest" / "results" / "phase_diag_gate"
OUT_DIR   = PROJECT_ROOT / "backtest" / "results" / "phase_diag_gate2"
ANNOT_DIR = OUT_DIR / "annotation_summary"
CHART_DIR = OUT_DIR / "charts"

ANNOT_DIR.mkdir(parents=True, exist_ok=True)
CHART_DIR.mkdir(parents=True, exist_ok=True)

NS_PER_SEC   = 1_000_000_000
MAX_LAG_SEC  = 300.0
P_CLOSE      = 0.70
ET           = "America/New_York"

# (key, display label, color)
EVENTS_DEF = [
    ("scanner_hit",  "Scanner Hit",      "royalblue"),
    ("warmup_end",   "Warmup End",        "gray"),
    ("first_pass",   "First PASS",        "limegreen"),
    ("deadline",     "Max Lag Deadline",  "orange"),
    ("entry",        "Entry",             "darkorange"),
    ("exit",         "Exit",              "crimson"),
]


def ns_to_naive_et(ns: int) -> pd.Timestamp:
    """Convert nanosecond UTC timestamp to naive ET wall-clock Timestamp."""
    return (
        pd.Timestamp(int(ns), unit="ns", tz="UTC")
        .tz_convert(ET)
        .tz_localize(None)
    )


def pass_intervals_ns(ticks: list[dict]) -> list[tuple[int, int]]:
    """Return list of (start_ns, end_ns) for each contiguous PASS stretch."""
    intervals: list[tuple[int, int]] = []
    in_pass = False
    start_ns = None
    prev_ns = None
    for t in ticks:
        gs = t["gate_state"]
        ts = t["ts_ns"]
        if gs == "PASS" and not in_pass:
            in_pass = True
            start_ns = ts
        elif gs != "PASS" and in_pass:
            in_pass = False
            intervals.append((start_ns, prev_ns))
        prev_ns = ts
    if in_pass and prev_ns is not None:
        intervals.append((start_ns, prev_ns))
    return intervals


def price_at(ts_naive: pd.Timestamp, bar_ts_naive: pd.DatetimeIndex, closes: list) -> float:
    """Close of the 30s bar whose start is just <= ts_naive."""
    idx = int(np.clip(bar_ts_naive.searchsorted(ts_naive, side="right") - 1, 0, len(closes) - 1))
    return float(closes[idx])


def assign_ay(events_sorted: list[dict]) -> None:
    """
    Assign pixel ay offsets in-place (negative = arrow points down from label).
    Events within 60 seconds of the previous one get staggered: -60 / -120.
    Isolated events get -80.
    """
    prev_ts = None
    prev_ay = -120
    for ev in events_sorted:
        if prev_ts is not None and abs((ev["ts_naive"] - prev_ts).total_seconds()) < 60:
            ev["ay"] = -120 if prev_ay == -60 else -60
        else:
            ev["ay"] = -80
        prev_ay = ev["ay"]
        prev_ts = ev["ts_naive"]


def compute_annotations(ev_meta: dict, replay: dict) -> tuple[dict, dict]:
    """
    T1: Derive the 6 annotation timestamps.
    Returns (annotation_summary dict for JSON, annot_events dict for chart).
    Performs T1a / T1b escalation checks (T1a is a hard stop).
    """
    ticks               = replay["ticks"]
    session_start_ns    = replay["session_start_ns"]
    scanner_hit_ns      = ev_meta["scanner_hit_ts_ns"]
    scanner_hit_t_sec   = replay["scanner_hit_t_sec"]

    # 1. Scanner Hit
    scanner_naive = ns_to_naive_et(scanner_hit_ns)

    # 2. Warmup End: first tick where gate_state not in {INACTIVE, WARMUP}
    warmup_end_ns = None
    for t in ticks:
        if t["gate_state"] not in ("INACTIVE", "WARMUP"):
            warmup_end_ns = t["ts_ns"]
            break
    warmup_naive = ns_to_naive_et(warmup_end_ns) if warmup_end_ns is not None else None
    t_warmup_end = ((warmup_end_ns - session_start_ns) / NS_PER_SEC
                    if warmup_end_ns is not None else None)

    # 3. First PASS post-scanner
    first_pass_ns = None
    for t in ticks:
        if t["gate_state"] == "PASS" and t["t_sec"] >= scanner_hit_t_sec:
            first_pass_ns = t["ts_ns"]
            break
    first_pass_naive = ns_to_naive_et(first_pass_ns) if first_pass_ns is not None else None
    t_first_pass = ((first_pass_ns - session_start_ns) / NS_PER_SEC
                    if first_pass_ns is not None else None)

    # 4. Max Lag Deadline
    deadline_ns    = scanner_hit_ns + int(MAX_LAG_SEC * NS_PER_SEC)
    deadline_naive = ns_to_naive_et(deadline_ns)

    # 5. Entry / Exit
    entry_ns    = ev_meta["entry_ts"]
    exit_ns     = ev_meta["exit_ts"]
    entry_naive = ns_to_naive_et(entry_ns)
    exit_naive  = ns_to_naive_et(exit_ns)

    t_scanner = scanner_hit_t_sec
    t_entry   = ev_meta["entry_t_sec"]
    t_exit    = ev_meta["exit_t_sec"]

    # T1a: entry before scanner — hard stop
    flag_t1a = t_entry < t_scanner
    # T1b: no PASS after scanner — note, continue
    flag_t1b = first_pass_ns is None

    summary = {
        "ticker":              replay["ticker"],
        "date":                replay["date"],
        "slot":                replay["slot"],
        "exit_reason":         ev_meta["exit_reason"],
        "pnl_pct":             ev_meta["pnl_pct"],
        "t_scanner_hit_sec":        t_scanner,
        "t_warmup_end_sec":         t_warmup_end,
        "t_first_pass_sec":         t_first_pass,
        "t_max_lag_deadline_sec":   t_scanner + MAX_LAG_SEC,
        "t_entry_sec":              t_entry,
        "t_exit_sec":               t_exit,
        "entry_lag_sec":            t_entry - t_scanner,
        "first_pass_lag_sec":       (t_first_pass - t_scanner) if t_first_pass is not None else None,
        "entry_price":         ev_meta["entry_price"],
        "exit_price":          ev_meta["exit_price"],
        "flag_t1a_entry_before_scanner": flag_t1a,
        "flag_t1b_no_pass_post_scanner": flag_t1b,
    }

    # annot_events: keyed by EVENTS_DEF key; ts_naive may be None (warmup, first_pass)
    annot_events = {
        "scanner_hit": {"ts_naive": scanner_naive,     "ts_ns": scanner_hit_ns},
        "warmup_end":  {"ts_naive": warmup_naive,      "ts_ns": warmup_end_ns},
        "first_pass":  {"ts_naive": first_pass_naive,  "ts_ns": first_pass_ns},
        "deadline":    {"ts_naive": deadline_naive,    "ts_ns": deadline_ns},
        "entry":       {"ts_naive": entry_naive,       "ts_ns": entry_ns},
        "exit":        {"ts_naive": exit_naive,        "ts_ns": exit_ns},
    }

    return summary, annot_events


def build_chart(ev_meta: dict, replay: dict, ohlc: dict,
                summary: dict, annot_events: dict) -> str:
    """T2: Build 2-panel Plotly HTML. Returns HTML string."""
    ticks  = replay["ticks"]
    ticker = replay["ticker"]
    date   = replay["date"]
    slot   = replay["slot"]

    # ── OHLC bars ─────────────────────────────────────────────────────────────
    bar_ts_raw = ohlc["bar_ts"]
    # Parse tz-aware strings ("2024-06-14 04:23:30-04:00"), convert to naive ET
    bar_ts_dt    = pd.to_datetime(bar_ts_raw)          # tz-aware, fixed offset
    bar_ts_naive = bar_ts_dt.tz_localize(None)         # strip tz → naive ET wall-clock

    opens  = ohlc["open"]
    highs  = ohlc["high"]
    lows   = ohlc["low"]
    closes = ohlc["close"]

    # ── Ratio trace (thinned) ─────────────────────────────────────────────────
    n    = len(ticks)
    step = max(1, n // 20_000) if n > 40_000 else 1
    ticks_thin   = ticks[::step]
    ratio_ts_ns  = np.array([t["ts_ns"] for t in ticks_thin], dtype=np.int64)
    ratio_vals   = np.array([t["ratio"]  for t in ticks_thin], dtype=np.float64)
    ratio_ts_naive = (
        pd.to_datetime(ratio_ts_ns, unit="ns", utc=True)
        .tz_convert(ET)
        .tz_localize(None)
    )

    # ── PASS shading intervals ────────────────────────────────────────────────
    pass_ivs_ns = pass_intervals_ns(ticks)
    pass_ivs_naive = [
        (ns_to_naive_et(s), ns_to_naive_et(e))
        for s, e in pass_ivs_ns
    ]

    # ── Figure ────────────────────────────────────────────────────────────────
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.70, 0.30],
        vertical_spacing=0.03,
        subplot_titles=[
            f"[{slot}] {ticker}  {date}  —  30s OHLC + EPG PASS + Annotations",
            "EPG Gate Ratio  (λ_v / λ_v_peak)",
        ],
    )

    # Panel 1: OHLC candlestick
    fig.add_trace(go.Candlestick(
        x=bar_ts_naive,
        open=opens, high=highs, low=lows, close=closes,
        name="OHLC",
        increasing_line_color="#26a69a",
        decreasing_line_color="#ef5350",
        showlegend=False,
    ), row=1, col=1)

    # Panel 2: ratio trace
    fig.add_trace(go.Scatter(
        x=ratio_ts_naive,
        y=ratio_vals,
        mode="lines",
        line=dict(color="#64b5f6", width=1),
        name="ratio",
    ), row=2, col=1)

    # PASS shading on both panels
    for s_naive, e_naive in pass_ivs_naive:
        fig.add_vrect(
            x0=s_naive, x1=e_naive,
            fillcolor="rgba(0,200,100,0.12)",
            line_width=0,
            layer="below",
            row="all",
        )

    # Dummy trace for PASS legend entry
    fig.add_trace(go.Scatter(
        x=[None], y=[None],
        mode="markers",
        marker=dict(symbol="square", size=10, color="rgba(0,200,100,0.35)"),
        name="EPG PASS",
    ), row=1, col=1)

    # Panel 2: p_close and peak reference lines
    fig.add_hline(
        y=P_CLOSE, line_dash="dash", line_color="red", line_width=1.2,
        annotation_text="p_close=0.70", annotation_position="bottom right",
        row=2, col=1,
    )
    fig.add_hline(
        y=1.0, line_dash="dash", line_color="#888", line_width=1,
        annotation_text="peak=1.0", annotation_position="top right",
        row=2, col=1,
    )

    # Panel 2: event vlines — NO annotation_text (Plotly 6 crashes on Timestamp x + ann_text)
    vline_defs = [
        ("scanner_hit", "royalblue"),
        ("entry",       "darkorange"),
        ("exit",        "crimson"),
    ]
    for key, color in vline_defs:
        ts_naive = annot_events[key]["ts_naive"]
        if ts_naive is not None:
            fig.add_vline(
                x=ts_naive.value,  # pass as int ns to avoid Timestamp ann_text bug
                line_color=color, line_dash="dot", line_width=1.2,
                row=2, col=1,
            )

    # ── Arrow annotations on Panel 1 ─────────────────────────────────────────
    # Build list, drop any with ts_naive=None
    ev_list = []
    for key, label, color in EVENTS_DEF:
        ts_naive = annot_events[key]["ts_naive"]
        if ts_naive is None:
            continue
        ev_list.append({
            "key":     key,
            "label":   label,
            "color":   color,
            "ts_naive": ts_naive,
        })

    ev_list.sort(key=lambda e: e["ts_naive"])
    assign_ay(ev_list)

    for ev in ev_list:
        ts_naive = ev["ts_naive"]
        y_price  = price_at(ts_naive, bar_ts_naive, closes)
        fig.add_annotation(
            x=ts_naive,
            y=y_price,
            text=ev["label"],
            showarrow=True,
            arrowhead=2,
            arrowwidth=2,
            arrowcolor=ev["color"],
            ax=0,
            ay=ev["ay"],
            font=dict(color=ev["color"], size=9),
            bgcolor="rgba(0,0,0,0.65)",
            bordercolor=ev["color"],
            borderwidth=1,
            row=1, col=1,
        )

    # ── Layout ────────────────────────────────────────────────────────────────
    pnl_s = f"+{ev_meta['pnl_pct']:.2f}%" if ev_meta["pnl_pct"] >= 0 else f"{ev_meta['pnl_pct']:.2f}%"
    fig.update_layout(
        title=dict(
            text=f"[{slot}] {ticker}  {date}  |  {ev_meta['exit_reason']}  |  PnL {pnl_s}",
            font=dict(size=14, color="#e0e0e0"),
        ),
        width=900,
        height=1400,
        paper_bgcolor="#0d0d0d",
        plot_bgcolor="#1a1a1a",
        font=dict(color="#e0e0e0"),
        showlegend=True,
        legend=dict(
            bgcolor="rgba(30,30,30,0.8)",
            bordercolor="#444",
            borderwidth=1,
            x=0.01, y=0.99,
        ),
        xaxis_rangeslider_visible=False,
    )
    fig.update_xaxes(gridcolor="#2a2a2a", tickformat="%H:%M")
    fig.update_yaxes(gridcolor="#2a2a2a")

    return fig.to_html(include_plotlyjs="cdn", full_html=True)


def main() -> None:
    sel_path = DIAG_DIR / "selected_events.json"
    with open(sel_path, encoding="utf-8") as f:
        events = json.load(f)

    summaries: list[dict] = []
    table_rows: list[tuple] = []

    for ev_meta in events:
        ticker = ev_meta["ticker"]
        date   = ev_meta["date"]
        slot   = ev_meta["slot"]

        print("=" * 60)
        print(f"  [{slot}] {ticker} {date}")

        replay_path = DIAG_DIR / "replay_data" / f"{ticker}_{date}.json"
        ohlc_path   = DIAG_DIR / "ohlc_30s"    / f"{ticker}_{date}.json"

        mb = replay_path.stat().st_size // 1_000_000
        print(f"  T1  loading replay ({mb}MB) ...", end=" ", flush=True)
        with open(replay_path, encoding="utf-8") as f:
            replay = json.load(f)
        print(f"({replay['n_ticks']:,} ticks)")

        print(f"  T1  loading ohlc ...", end=" ", flush=True)
        with open(ohlc_path, encoding="utf-8") as f:
            ohlc = json.load(f)
        print(f"({ohlc['n_bars']} bars)")

        print(f"  T1  computing annotations ...", end=" ", flush=True)
        summary, annot_events = compute_annotations(ev_meta, replay)
        print("OK")

        # Escalation checks
        if summary["flag_t1a_entry_before_scanner"]:
            print(f"  !! T1a HARD STOP: entry t={summary['t_entry_sec']:.1f}s is BEFORE scanner t={summary['t_scanner_hit_sec']:.1f}s")
            sys.exit(1)
        if summary["flag_t1b_no_pass_post_scanner"]:
            print(f"  !! T1b NOTE: no PASS after scanner hit -- First PASS arrow omitted")

        lag_e = summary["entry_lag_sec"]
        lag_p = summary["first_pass_lag_sec"]
        lag_p_s = f"{lag_p:.1f}s" if lag_p is not None else "None"
        print(f"       entry_lag={lag_e:.1f}s  first_pass_lag={lag_p_s}")

        # Write annotation summary
        annot_path = ANNOT_DIR / f"{ticker}_{date}.json"
        with open(annot_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, default=str)
        print(f"  T1  annotation_summary -> {annot_path.name}")

        summaries.append(summary)
        table_rows.append((slot, ticker, date, ev_meta["exit_reason"],
                           ev_meta["pnl_pct"], lag_e, lag_p))

        # T2: chart
        chart_path = CHART_DIR / f"{ticker}_{date}.html"
        print(f"  T2  building chart ...", end=" ", flush=True)
        html = build_chart(ev_meta, replay, ohlc, summary, annot_events)
        with open(chart_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"OK -> {chart_path.name}")

    # T3: index.html
    print("\nT3  writing index2.html ...", end=" ", flush=True)
    rows_html = ""
    for slot, ticker, date, exit_r, pnl, lag_e, lag_p in table_rows:
        pnl_s   = f"+{pnl:.2f}%" if pnl >= 0 else f"{pnl:.2f}%"
        lag_e_s = f"{lag_e:.1f}s"
        lag_p_s = f"{lag_p:.1f}s" if lag_p is not None else "&mdash;"
        fn      = f"{ticker}_{date}.html"
        rows_html += (
            f"\n  <tr>"
            f"<td>{slot}</td><td>{ticker}</td><td>{date}</td>"
            f"<td>{exit_r}</td>"
            f"<td class='num'>{pnl_s}</td>"
            f"<td class='num'>{lag_e_s}</td>"
            f"<td class='num'>{lag_p_s}</td>"
            f"<td><a href='{fn}' target='_blank'>chart</a></td>"
            f"</tr>"
        )

    index_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>DIAG-GATE-2 -- Annotated Entry/Exit Charts</title>
<style>
  body{{background:#0d0d0d;color:#e0e0e0;font-family:monospace;padding:20px}}
  h1{{color:#90caf9;margin-bottom:4px}}
  p{{color:#9e9e9e;margin-top:0}}
  table{{border-collapse:collapse;width:100%;max-width:880px}}
  th,td{{padding:6px 12px;text-align:left;border-bottom:1px solid #2a2a2a}}
  th{{color:#90caf9;border-bottom:2px solid #444}}
  tr:hover td{{background:#1e1e1e}}
  a{{color:#80cbc4;text-decoration:none}}
  a:hover{{text-decoration:underline}}
  .num{{text-align:right;font-variant-numeric:tabular-nums}}
</style>
</head>
<body>
<h1>Phase DIAG-GATE-2 -- Annotated Entry/Exit Charts</h1>
<p>2-panel Plotly charts: 30s OHLC + EPG PASS shading + labeled arrows // gate ratio + p_close.
   MDR&ge;150 sample, R1 p=0.70 selections.</p>
<table>
<thead>
  <tr>
    <th>Slot</th><th>Ticker</th><th>Date</th><th>Exit Reason</th>
    <th class="num">PnL%</th><th class="num">Entry Lag</th>
    <th class="num">First PASS Lag</th><th>Chart</th>
  </tr>
</thead>
<tbody>{rows_html}
</tbody>
</table>
</body>
</html>"""

    index_path = CHART_DIR / "index2.html"
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(index_html)
    print(f"OK -> {index_path}")

    # Final report
    print("\n" + "=" * 72)
    print("DIAG-GATE-2 annotation summary")
    print("=" * 72)
    hdr = f"{'Slot':<5} {'Ticker':<7} {'Date':<12} {'Exit':<22} {'PnL%':>8}  {'EntryLag':>9}  {'PassLag':>9}"
    print(hdr)
    print("-" * len(hdr))
    for s in summaries:
        pnl_s = f"+{s['pnl_pct']:.2f}%" if s["pnl_pct"] >= 0 else f"{s['pnl_pct']:.2f}%"
        e_s   = f"{s['entry_lag_sec']:.1f}s"
        p_s   = f"{s['first_pass_lag_sec']:.1f}s" if s["first_pass_lag_sec"] is not None else "None"
        print(f"{s['slot']:<5} {s['ticker']:<7} {s['date']:<12} {s['exit_reason']:<22} {pnl_s:>8}  {e_s:>9}  {p_s:>9}")
    print()
    print(f"Output:")
    print(f"  Annotation summaries : {ANNOT_DIR}")
    print(f"  Charts               : {CHART_DIR}")
    print(f"  Index                : {index_path}")
    print("Done.")


if __name__ == "__main__":
    main()
