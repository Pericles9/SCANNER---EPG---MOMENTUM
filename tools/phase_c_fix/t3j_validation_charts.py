"""T3j: Phase C.5 visual spot-check charts.

Writes 3 charts comparing buggy vs fixed CVD at rising edges:
  1. XBP 2023-12-04 — uptrend (+70.8%)
  2. XBP 2023-12-14 — backside (gap-down from $37.5 to $7.0 open)
  3. SLDB 2023-12-07 — mixed (uptrend with sell pressure)

Each chart: Price | CVD (buggy vs fixed) | EPG state
Escalation rule: if uptrending event CVD is persistently negative at >50% of rising
  edges under the FIXED accumulator → hard stop.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.exit_d_tuning.replay import _load_cache
from data.loaders.trades import load_trades, list_events

_ROOT = Path(__file__).resolve().parents[2]
_PHASE_U_CACHE = Path(r"D:\Trading Research\hawkes-ofi-impact\results\phase_u\replay_caches")
_PHASE_B_CACHE = _ROOT / "results" / "phase_b" / "replay_caches"
_AUDIT_PATH = (_ROOT / "results" / "phase_b" / "100_val_seed42"
               / "event_charts" / "cache_audit.json")
_OUT_DIR = _ROOT / "results" / "phase_c_fix" / "validation_charts"

EPG_PASS = 2

EVENTS = [
    ("XBP",  "2023-12-04", "uptrend",  "Strong uptrend (+70.8% from T_event to session end)"),
    ("XBP",  "2023-12-14", "backside", "Backside session (gap-down from $37.5 close to $7.0 open)"),
    ("SLDB", "2023-12-07", "mixed",    "Mixed: uptrend with sell pressure (11/21 CVD-blocks)"),
]


def _get_cache_source(ticker, date, audit_map):
    return audit_map.get((ticker, date), "fallback")


def _load_replay_for(ticker, date, audit_map):
    src = _get_cache_source(ticker, date, audit_map)
    if src == "phase_b":
        r = _load_cache(_PHASE_B_CACHE, ticker, date)
    elif src == "phase_u":
        r = _load_cache(_PHASE_U_CACHE, ticker, date)
    else:
        r = _load_cache(_PHASE_B_CACHE, ticker, date) or _load_cache(_PHASE_U_CACHE, ticker, date)
    return r


def _compute_cvd(prices, sizes, sides, t_event_idx, N, buggy: bool):
    arr = np.zeros(N)
    running = 0.0
    for i in range(t_event_idx, N):
        if buggy:
            direction = 1.0 if sides[i] == 1 else -1.0
        else:
            direction = float(sides[i])
        running += float(prices[i]) * float(sizes[i]) * direction
        arr[i] = running
    return arr


def build_chart(ticker, date, label, description, replay, sizes):
    ts = replay.timestamps_ns
    prices = replay.prices
    sides = replay.sides
    epg = replay.epg_state
    t_event_ns = replay.t_event_ns
    N = len(ts)

    t_event_idx = int(np.searchsorted(ts, t_event_ns)) if t_event_ns else 0
    t_event_ns_f = int(t_event_ns) if t_event_ns else int(ts[0])

    # Compute CVDs
    cvd_buggy = _compute_cvd(prices, sizes, sides, t_event_idx, N, buggy=True)
    cvd_fixed = _compute_cvd(prices, sizes, sides, t_event_idx, N, buggy=False)

    # Convert timestamps to seconds from t_event
    t_sec = (ts - t_event_ns_f) / 1e9

    # Find rising edges after T_event
    rising_edges = []
    for i in range(1, N):
        if epg[i] == EPG_PASS and epg[i - 1] != EPG_PASS and i >= t_event_idx:
            rising_edges.append(i)

    # EPG state as numeric (0=INACTIVE,1=WARMUP,2=PASS,3=FAIL)
    epg_state_num = epg.astype(float)

    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        row_heights=[0.45, 0.35, 0.20],
        vertical_spacing=0.05,
        subplot_titles=["Price", "CVD since T_event", "EPG state"],
    )

    # ── Row 1: Price ──────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=t_sec, y=prices, mode="lines", name="Price",
        line=dict(color="#2266cc", width=1),
    ), row=1, col=1)

    # Mark rising edges on price panel
    for j in rising_edges:
        fixed_ok = cvd_fixed[j] >= 0
        buggy_ok = cvd_buggy[j] >= 0
        if fixed_ok and buggy_ok:
            color = "#00aa44"  # both pass
        elif fixed_ok and not buggy_ok:
            color = "#ff8800"  # fixed passes, buggy wrongly blocked
        elif not fixed_ok and buggy_ok:
            color = "#cc0000"  # fixed blocks, buggy wrongly passes
        else:
            color = "#cc0000"  # both block
        fig.add_vline(x=float(t_sec[j]), line_width=1, line_dash="dash",
                      line_color=color, row=1, col=1)

    # ── Row 2: CVD ───────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=t_sec, y=cvd_buggy, mode="lines", name="CVD buggy",
        line=dict(color="#cc4444", width=1.5, dash="dot"),
    ), row=2, col=1)
    fig.add_trace(go.Scatter(
        x=t_sec, y=cvd_fixed, mode="lines", name="CVD fixed",
        line=dict(color="#228866", width=1.5),
    ), row=2, col=1)
    fig.add_hline(y=0, line_width=1, line_color="#888", row=2, col=1)

    # Rising edge markers on CVD
    for j in rising_edges:
        fig.add_scatter(
            x=[t_sec[j]], y=[cvd_fixed[j]], mode="markers",
            marker=dict(symbol="triangle-up", size=10,
                        color="#228866" if cvd_fixed[j] >= 0 else "#cc0000"),
            name="edge (fixed)", showlegend=False,
            row=2, col=1,
        )

    # ── Row 3: EPG state ─────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=t_sec, y=epg_state_num, mode="lines", name="EPG state",
        line=dict(color="#8844cc", width=1),
        fill="tozeroy", fillcolor="rgba(136,68,204,0.15)",
    ), row=3, col=1)

    # Annotate rising edges where buggy and fixed disagree
    n_fixed_pass = sum(1 for j in rising_edges if cvd_fixed[j] >= 0)
    n_fixed_block = sum(1 for j in rising_edges if cvd_fixed[j] < 0)
    n_bug_flipped = sum(1 for j in rising_edges
                        if (cvd_fixed[j] >= 0) != (cvd_buggy[j] >= 0))

    title = (
        f"{ticker} {date} — {label}<br>"
        f"<sub>{description}</sub><br>"
        f"<sub>{len(rising_edges)} rising edges: {n_fixed_pass} PASS / {n_fixed_block} BLOCK (fixed) | "
        f"{n_bug_flipped} decision changes vs buggy</sub>"
    )

    fig.update_layout(
        title=title,
        height=600,
        legend=dict(orientation="h", y=-0.05),
        margin=dict(t=100, b=60, l=60, r=20),
    )
    fig.update_xaxes(title_text="Seconds since T_event", row=3, col=1)
    fig.update_yaxes(title_text="Price ($)", row=1, col=1)
    fig.update_yaxes(title_text="CVD ($·shares)", row=2, col=1)
    fig.update_yaxes(title_text="State (0-3)", row=3, col=1,
                     tickvals=[0, 1, 2, 3],
                     ticktext=["INACT", "WARM", "PASS", "FAIL"])

    return fig, n_fixed_pass, n_fixed_block, n_bug_flipped


def main():
    _OUT_DIR.mkdir(parents=True, exist_ok=True)

    with open(_AUDIT_PATH) as f:
        audit = json.load(f)
    audit_map = {(a["ticker"], a["date"]): a["cache_source"] for a in audit}

    mom_map = {(ev["ticker"], ev["date"]): ev["mom_pct"]
               for ev in list_events(min_mom=0.0, require_date=True)}

    check_d_results = []
    escalation_triggered = False

    for ticker, date, label, description in EVENTS:
        print(f"Building chart: {ticker} {date} ({label})")
        replay = _load_replay_for(ticker, date, audit_map)
        if replay is None:
            print(f"  ERROR: no replay cache for {ticker} {date}")
            continue

        mom_pct = mom_map.get((ticker, date))
        td = load_trades(ticker, date, mom_pct)
        sizes = td.sizes.astype(np.float64)

        fig, n_pass, n_block, n_flipped = build_chart(
            ticker, date, label, description, replay, sizes
        )

        out_path = _OUT_DIR / f"{ticker}_{date}_{label}.html"
        fig.write_html(str(out_path), include_plotlyjs="cdn")
        print(f"  Written: {out_path.name}")
        print(f"  Edges: {n_pass + n_block} total, {n_pass} PASS / {n_block} BLOCK (fixed), "
              f"{n_flipped} decision changes")

        # Escalation: uptrending event with CVD persistently negative at >50% of rising edges
        if label == "uptrend":
            pct_blocked = n_block / (n_pass + n_block) if (n_pass + n_block) > 0 else 0
            if pct_blocked > 0.50:
                escalation_triggered = True
                print(f"  ESCALATION: {pct_blocked:.0%} of rising edges CVD-blocked under fixed (>50% threshold)")
            else:
                print(f"  Uptrend check: {pct_blocked:.0%} edges CVD-blocked — CLEAR")

        check_d_results.append({
            "ticker": ticker,
            "date": date,
            "label": label,
            "n_rising_edges": n_pass + n_block,
            "n_pass_fixed": n_pass,
            "n_block_fixed": n_block,
            "n_decision_changes_vs_buggy": n_flipped,
            "chart_file": out_path.name,
        })

    result = {
        "check_d_visual_spot_check": {
            "escalation_triggered": escalation_triggered,
            "escalation_rule": ("Uptrending event CVD persistently negative "
                                "at >50% of rising edges under fixed accumulator"),
            "result": "ESCALATION" if escalation_triggered else "CLEAR",
            "events": check_d_results,
        }
    }
    out_json = _OUT_DIR / "check_d_results.json"
    with open(out_json, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\nCheck D overall: {'ESCALATION' if escalation_triggered else 'CLEAR'}")
    print(f"Written: {out_json}")
    return not escalation_triggered


if __name__ == "__main__":
    main()
