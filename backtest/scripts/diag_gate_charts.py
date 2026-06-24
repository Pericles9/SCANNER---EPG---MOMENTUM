#!/usr/bin/env python3
"""
Phase DIAG-GATE — T2 through T5.

Replays each of the 5 selected events through the full runner_rapid gate
pipeline, capturing tick-level gate internals, then builds diagnostic charts.

T2:  Tick-level gate replay  -> replay_data/{TICKER}_{DATE}.json
T2b: Entry-lag assertion (t_entry >= t_scanner_hit)
T3:  30s OHLC with carry-forward -> ohlc_30s/{TICKER}_{DATE}.json
T4:  4-panel Plotly HTML (1200x1400px) -> charts/{TICKER}_{DATE}.html
T5:  Index page -> charts/index.html

Usage:
    python backtest/scripts/diag_gate_charts.py
"""
from __future__ import annotations

import json
import math
import sys
import textwrap
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# runner_rapid.py uses both "from backtest.xxx" and bare "from data.xxx" imports;
# we need PROJECT_ROOT so "backtest" resolves as a package, and PROJECT_ROOT/backtest
# so bare module names (data.schemas, core.epg, …) also resolve.
sys.path.insert(0, str(PROJECT_ROOT / "backtest"))
sys.path.insert(0, str(PROJECT_ROOT))

from data.schemas.mom_db import CONFIG_DIR, NS_PER_SECOND  # noqa
from data.loaders.trades import load_trades, compute_lambda_ref_per_event  # noqa
from data.loaders.quotes import load_quotes  # noqa
from core.ofi.trade_ofi import compute_trade_ofi  # noqa
from core.epg.anchor import EventAnchor  # noqa
from core.epg.gate import ParticipationGate, GateState  # noqa
from runner_rapid import _hawkes_replay_with_refit, _build_halt_intervals, EPG_K, EPG_TAU, EPG_WARMUP  # noqa

DIAG_DIR   = PROJECT_ROOT / "backtest" / "results" / "phase_diag_gate"
SELECTED   = DIAG_DIR / "selected_events.json"
REPLAY_DIR = DIAG_DIR / "replay_data"
OHLC_DIR   = DIAG_DIR / "ohlc_30s"
CHART_DIR  = DIAG_DIR / "charts"

EPG_P_OPEN   = 0.70
EPG_P_CLOSE  = 0.70
EPG_TAU_PEAK = 600.0
EPG_C        = 2.0
ET           = "America/New_York"


# ── Config ────────────────────────────────────────────────────────────────

def _load_config() -> tuple[dict, dict]:
    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar = json.load(f)
    return hawkes, q_bar


# ── T2: Gate Replay ───────────────────────────────────────────────────────

def _replay_event(ev: dict, hawkes_median: dict, q_bar_cfg: dict) -> dict:
    """Run the full runner_rapid pipeline and capture tick-level gate internals."""
    ticker  = ev["ticker"]
    date    = ev["date"]
    mom_pct = ev["mom_pct"]

    td = load_trades(ticker, date, mom_pct)
    if td is None or td.n_trades < 30:
        raise RuntimeError(f"Insufficient trades for {ticker} {date}")

    qd = load_quotes(ticker, date, mom_pct)
    if qd is None or qd.n_quotes < 10:
        raise RuntimeError(f"Insufficient quotes for {ticker} {date}")

    N = td.n_trades

    # OFI sides (Lee-Ready)
    tier_qbar = q_bar_cfg.get("wide", {}).get("median", 250.0)
    ofi = compute_trade_ofi(
        trade_timestamps=td.timestamps,
        trade_prices=td.prices,
        trade_sizes=td.sizes.astype(np.float64),
        quote_timestamps=qd.timestamps,
        quote_bid_prices=qd.bid_prices,
        quote_ask_prices=qd.ask_prices,
        quote_bid_sizes=qd.bid_sizes.astype(np.float64),
        quote_ask_sizes=qd.ask_sizes.astype(np.float64),
        window_sec=10.0,
        q_bar_fallback=tier_qbar,
    )

    # Halt intervals (C3 halt-gap pause)
    halt_intervals = _build_halt_intervals(td)

    # Hawkes replay with online refit
    lam_buy  = np.zeros(N, dtype=np.float64)
    lam_sell = np.zeros(N, dtype=np.float64)
    E_out    = np.zeros(N, dtype=np.float64)
    Edot_out = np.zeros(N, dtype=np.float64)
    nbase    = np.zeros(N, dtype=np.float64)
    dv_arr   = td.prices.astype(np.float64) * td.sizes.astype(np.float64)

    fp  = hawkes_median
    rho = hawkes_median.get("rho", 0.99)

    global_lref = fp["mu_buy"] + fp["mu_sell"]
    per_ev_lref = compute_lambda_ref_per_event(ticker, date)
    lambda_ref  = global_lref if (math.isnan(per_ev_lref) or per_ev_lref <= 0) else per_ev_lref

    cold_params = _hawkes_replay_with_refit(
        t_sec=td.t_sec, sides=ofi.sides,
        rho=rho, lambda_ref=lambda_ref,
        init_params=fp, rho_E=rho,
        lam_buy_out=lam_buy, lam_sell_out=lam_sell,
        E_out=E_out, Edot_out=Edot_out, n_base_out=nbase,
        dv_arr=dv_arr,
        halt_intervals=halt_intervals or None,
    )
    lambda_hat = lam_buy + lam_sell

    # EventAnchor -> T_event
    global_lref_epg = fp["mu_buy"] + fp["mu_sell"]
    anchor = EventAnchor(lambda_ref=global_lref_epg, k_multiplier=EPG_K)
    if cold_params is not None:
        lref_epg = cold_params.mu_buy + cold_params.mu_sell
        if lref_epg > 0:
            anchor.set_lambda_ref(lref_epg)

    # Gate (peak mode, p_open=p_close=0.70, warmup=300s, tau=300s)
    gate = ParticipationGate(
        half_life_seconds=EPG_TAU,
        peak_threshold_p=EPG_P_OPEN,
        warmup_seconds=EPG_WARMUP,
        gate_mode="peak",
        tau_peak=EPG_TAU_PEAK,
        C=EPG_C,
        p_open=EPG_P_OPEN,
        p_close=EPG_P_CLOSE,
    )

    ticks          = []
    t_event_fired  = False
    t_event_sec    = None

    for i in range(N):
        t_ev = anchor.update(lambda_hat[i], td.t_sec[i])
        if t_ev is not None and not t_event_fired:
            gate.activate(t_ev)
            t_event_fired = True
            t_event_sec   = float(td.t_sec[i])

        dv    = float(td.prices[i]) * float(td.sizes[i])
        state = gate.update(dv, td.t_sec[i])

        lv      = gate.lambda_v
        lv_peak = gate.lambda_v_peak
        ratio   = (lv / lv_peak) if lv_peak > 0 else 0.0

        ticks.append({
            "i":               i,
            "ts_ns":           int(td.timestamps[i]),
            "t_sec":           float(td.t_sec[i]),
            "price":           float(td.prices[i]),
            "size":            int(td.sizes[i]),
            "dv":              float(dv),
            "lambda_v":        float(lv),
            "lambda_v_peak":   float(lv_peak),
            "threshold":       float(gate.threshold),
            "threshold_close": float(gate.threshold_close),
            "ratio":           float(ratio),
            "gate_state":      state.value,
        })

    # Scanner hit in t_sec units (seconds from first trade)
    scanner_hit_t_sec = (ev["scanner_hit_ts_ns"] - int(td.timestamps[0])) / NS_PER_SECOND

    return {
        "ticker":            ticker,
        "date":              date,
        "slot":              ev["slot"],
        "dir_name":          ev["dir_name"],
        "session_start_ns":  int(td.timestamps[0]),
        "t_event_fired":     t_event_fired,
        "t_event_sec":       t_event_sec,
        "scanner_hit_t_sec": float(scanner_hit_t_sec),
        "n_ticks":           N,
        "ticks":             ticks,
    }


# ── T3: 30s OHLC with carry-forward ──────────────────────────────────────

def _build_ohlc_30s(replay: dict) -> dict:
    """Build 30s OHLC bars from tick data. Carry-forward for empty bars."""
    ticks = replay["ticks"]

    ts_et  = pd.to_datetime([t["ts_ns"] for t in ticks], unit="ns", utc=True).tz_convert(ET)
    prices = np.array([t["price"] for t in ticks])
    sizes  = np.array([t["size"]  for t in ticks])
    dvs    = np.array([t["dv"]    for t in ticks])

    df = pd.DataFrame({"price": prices, "size": sizes, "dv": dvs}, index=ts_et)

    ohlc = df["price"].resample("30s").agg(
        Open=("first"), High=("max"), Low=("min"), Close=("last")
    )
    vol   = df["size"].resample("30s").sum()
    dv30  = df["dv"].resample("30s").sum()
    cnt30 = df["price"].resample("30s").count()

    # Carry-forward for empty bars: use previous Close for all OHLC
    close_cf = ohlc["Close"].ffill()
    empty    = ohlc["Open"].isna()
    for col in ("Open", "High", "Low", "Close"):
        ohlc.loc[empty, col] = close_cf[empty]

    # Drop leading NaN rows (before any trades)
    ohlc  = ohlc.dropna(subset=["Open"])
    vol   = vol.reindex(ohlc.index).fillna(0)
    dv30  = dv30.reindex(ohlc.index).fillna(0.0)
    cnt30 = cnt30.reindex(ohlc.index).fillna(0)

    # Rolling trade counts (60s = 2 bars, 300s = 10 bars)
    roll60  = cnt30.rolling(2,  min_periods=1).sum()
    roll300 = cnt30.rolling(10, min_periods=1).sum()

    return {
        "bar_ts":   ohlc.index.astype(str).tolist(),
        "open":     ohlc["Open"].tolist(),
        "high":     ohlc["High"].tolist(),
        "low":      ohlc["Low"].tolist(),
        "close":    ohlc["Close"].tolist(),
        "volume":   vol.tolist(),
        "dv30":     dv30.tolist(),
        "count30":  cnt30.tolist(),
        "roll60":   roll60.tolist(),
        "roll300":  roll300.tolist(),
        "n_bars":   len(ohlc),
    }


# ── T4: 4-panel Plotly chart ──────────────────────────────────────────────

def _pass_intervals_et(ticks: list[dict]) -> list[tuple]:
    """Find contiguous PASS tick spans, return as (start_ns, end_ns) pairs."""
    intervals = []
    in_pass   = False
    start_ns  = None
    for t in ticks:
        if t["gate_state"] == "PASS" and not in_pass:
            in_pass  = True
            start_ns = t["ts_ns"]
        elif t["gate_state"] != "PASS" and in_pass:
            in_pass = False
            intervals.append((start_ns, t["ts_ns"]))
    if in_pass:
        intervals.append((start_ns, ticks[-1]["ts_ns"]))
    return intervals


def _ts(ns: int) -> pd.Timestamp:
    return pd.Timestamp(ns, unit="ns", tz="UTC").tz_convert(ET)


def _make_chart(ev_meta: dict, replay: dict, ohlc: dict) -> str:
    """Build 4-panel Plotly HTML. Returns HTML string."""
    ticker     = replay["ticker"]
    date       = replay["date"]
    slot       = replay["slot"]
    ticks      = replay["ticks"]

    # ── Datetime arrays ──
    ts_arr = pd.to_datetime([t["ts_ns"] for t in ticks], unit="ns", utc=True).tz_convert(ET)

    lv      = np.array([t["lambda_v"]      for t in ticks])
    lv_peak = np.array([t["lambda_v_peak"] for t in ticks])
    thresh  = np.array([t["threshold"]     for t in ticks])
    ratio   = np.array([t["ratio"]         for t in ticks])

    bar_ts  = pd.to_datetime(ohlc["bar_ts"])

    # ── Event timestamps ──
    scanner_ts = _ts(ev_meta["scanner_hit_ts_ns"])
    entry_ts   = _ts(ev_meta["entry_ts"])
    exit_ts    = _ts(ev_meta["exit_ts"])

    # ── Layout ──
    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.35, 0.28, 0.18, 0.19],
        specs=[
            [{"secondary_y": False}],
            [{"secondary_y": True}],
            [{"secondary_y": False}],
            [{"secondary_y": False}],
        ],
        subplot_titles=[
            f"[{slot}] {ticker} {date}  |  30s OHLC — PASS regions shaded",
            "Gate Intensity  (λ_v / λ_v_peak / threshold)",
            "Dollar Volume per 30s bar",
            "Rolling Trade Count (60s = solid, 300s = dashed)",
        ],
    )

    # ── Panel 1: Candlestick + PASS shading ──
    fig.add_trace(
        go.Candlestick(
            x=bar_ts,
            open=ohlc["open"], high=ohlc["high"],
            low=ohlc["low"],   close=ohlc["close"],
            name="OHLC",
            increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350",
            increasing_fillcolor="#26a69a",
            decreasing_fillcolor="#ef5350",
            showlegend=False,
        ),
        row=1, col=1,
    )

    for start_ns, end_ns in _pass_intervals_et(ticks):
        fig.add_vrect(
            x0=_ts(start_ns), x1=_ts(end_ns),
            fillcolor="rgba(0,200,0,0.07)",
            line_width=0,
            row=1, col=1,
        )

    # ── Panel 2: gate intensity (primary y) + ratio (secondary y) ──
    # Thin tick data if very large (> 40k points -> sample to ~20k)
    if len(ts_arr) > 40_000:
        step = len(ts_arr) // 20_000
        ts_s   = ts_arr[::step]
        lv_s   = lv[::step]
        lvp_s  = lv_peak[::step]
        thr_s  = thresh[::step]
        rat_s  = ratio[::step]
    else:
        ts_s, lv_s, lvp_s, thr_s, rat_s = ts_arr, lv, lv_peak, thresh, ratio

    fig.add_trace(
        go.Scatter(x=ts_s, y=lv_s, name="λ_v", mode="lines",
                   line=dict(color="#2196F3", width=1.5)),
        row=2, col=1,
    )
    fig.add_trace(
        go.Scatter(x=ts_s, y=lvp_s, name="λ_v peak", mode="lines",
                   line=dict(color="#FF9800", width=1.5, dash="dash")),
        row=2, col=1,
    )
    fig.add_trace(
        go.Scatter(x=ts_s, y=thr_s, name="threshold (open)", mode="lines",
                   line=dict(color="#F44336", width=1.0, dash="dash")),
        row=2, col=1,
    )
    # Ratio on secondary y
    fig.add_trace(
        go.Scatter(x=ts_s, y=rat_s, name="ratio λ_v/peak", mode="lines",
                   line=dict(color="#9E9E9E", width=1.0)),
        row=2, col=1, secondary_y=True,
    )
    # p_close reference line on secondary y (constant=0.70)
    fig.add_trace(
        go.Scatter(
            x=[ts_arr[0], ts_arr[-1]],
            y=[EPG_P_CLOSE, EPG_P_CLOSE],
            mode="lines",
            name=f"p_close={EPG_P_CLOSE}",
            line=dict(color="rgba(244,67,54,0.6)", width=1.0, dash="dot"),
            showlegend=True,
        ),
        row=2, col=1, secondary_y=True,
    )

    # ── Panel 3: dollar volume per 30s bar ──
    fig.add_trace(
        go.Bar(
            x=bar_ts,
            y=ohlc["dv30"],
            name="DV 30s",
            marker_color="#546E7A",
            showlegend=False,
        ),
        row=3, col=1,
    )

    # ── Panel 4: rolling trade count ──
    fig.add_trace(
        go.Scatter(x=bar_ts, y=ohlc["roll60"], name="Roll 60s count", mode="lines",
                   line=dict(color="#CE93D8", width=1.5)),
        row=4, col=1,
    )
    fig.add_trace(
        go.Scatter(x=bar_ts, y=ohlc["roll300"], name="Roll 300s count", mode="lines",
                   line=dict(color="#7B1FA2", width=1.5, dash="dash")),
        row=4, col=1,
    )

    # ── Vertical event lines (all panels) ──
    event_lines = [
        (scanner_ts, "#2196F3", "Scanner hit"),
        (entry_ts,   "#FF9800", "Entry"),
        (exit_ts,    "#F44336", "Exit"),
    ]
    for ts_val, color, label in event_lines:
        for row in (1, 2, 3, 4):
            fig.add_vline(
                x=ts_val,
                line=dict(color=color, width=1.5, dash="dot"),
                row=row, col=1,
            )

    # ── Secondary y-axis range (ratio: 0 to 1.15) ──
    fig.update_yaxes(range=[0, 1.15], row=2, col=1, secondary_y=True,
                     title_text="ratio", showgrid=False)
    fig.update_yaxes(title_text="λ", row=2, col=1, secondary_y=False)
    fig.update_yaxes(title_text="price ($)", row=1, col=1)
    fig.update_yaxes(title_text="DV ($)", row=3, col=1)
    fig.update_yaxes(title_text="count", row=4, col=1)

    # ── Dark mode layout ──
    pnl_pct = ev_meta.get("pnl_pct", 0.0)
    exit_reason = ev_meta.get("exit_reason", "")
    title_text = (
        f"[{slot}] {ticker} {date}  |  mom={ev_meta['mom_pct']:.1f}%  "
        f"PnL={pnl_pct:+.2f}%  exit={exit_reason}  (p_open=p_close={EPG_P_CLOSE})"
    )

    fig.update_layout(
        template="plotly_dark",
        height=1400,
        width=1200,
        title=dict(text=title_text, font=dict(size=13), x=0.01),
        showlegend=True,
        legend=dict(
            x=1.06, y=1.0, bgcolor="rgba(0,0,0,0.4)",
            font=dict(size=10), borderwidth=0,
        ),
        xaxis_rangeslider_visible=False,
        xaxis4_rangeslider_visible=False,
        margin=dict(l=60, r=180, t=60, b=40),
    )

    # Disable range slider on all x-axes
    for i in range(1, 5):
        key = f"xaxis{'' if i == 1 else i}"
        fig.update_layout(**{key: dict(rangeslider_visible=False)})

    return fig.to_html(full_html=True, include_plotlyjs="cdn")


# ── T2b: Escalation check ─────────────────────────────────────────────────

def _t2b_check(ev_meta: dict, replay: dict) -> bool:
    """Assert entry_t_sec >= scanner_hit_t_sec. Returns True if OK."""
    entry_t_sec  = ev_meta.get("entry_t_sec")
    scanner_t_sec = replay.get("scanner_hit_t_sec")
    if entry_t_sec is None or scanner_t_sec is None:
        print(f"  T2b WARN [{ev_meta['slot']}] missing timestamps")
        return False
    ok = entry_t_sec >= scanner_t_sec
    lag = entry_t_sec - scanner_t_sec
    status = "OK" if ok else "FAIL"
    print(f"  T2b [{ev_meta['slot']}] {ev_meta['ticker']} {ev_meta['date']}: "
          f"entry_t_sec={entry_t_sec:.1f}  scanner_t_sec={scanner_t_sec:.1f}  "
          f"lag={lag:.1f}s  -> {status}")
    return ok


# ── T5: index.html ────────────────────────────────────────────────────────

def _make_index(selected: list[dict]) -> str:
    rows = []
    for ev in selected:
        ticker     = ev["ticker"]
        date       = ev["date"]
        slot       = ev["slot"]
        exit_r     = ev.get("exit_reason", "—")
        pnl        = ev.get("pnl_pct", 0.0)
        pnl_str    = f"{pnl:+.2f}%"
        fname      = f"{ticker}_{date}.html"
        rows.append(
            f"<tr>"
            f"<td>{slot}</td>"
            f"<td>{ticker}</td>"
            f"<td>{date}</td>"
            f"<td>{exit_r}</td>"
            f"<td style='text-align:right'>{pnl_str}</td>"
            f"<td><a href='{fname}' target='_blank'>{fname}</a></td>"
            f"</tr>"
        )
    tbody = "\n".join(rows)
    return textwrap.dedent(f"""\
        <!DOCTYPE html>
        <html>
        <head>
          <meta charset="utf-8">
          <title>DIAG-GATE charts</title>
          <style>
            body {{ font-family: monospace; background:#111; color:#ddd; padding:2em }}
            h2   {{ color:#eee }}
            table {{ border-collapse:collapse; width:auto }}
            th,td {{ border:1px solid #333; padding:6px 12px }}
            th {{ background:#222; color:#aaa }}
            a  {{ color:#90caf9 }}
          </style>
        </head>
        <body>
          <h2>Phase DIAG-GATE — Gate Intensity Charts</h2>
          <p>p_open = p_close = {EPG_P_OPEN}  |  EPG tau = {EPG_TAU}s  |  warmup = {EPG_WARMUP}s</p>
          <table>
            <thead>
              <tr><th>Slot</th><th>Ticker</th><th>Date</th><th>Exit Reason</th><th>PnL%</th><th>Chart</th></tr>
            </thead>
            <tbody>
        {tbody}
            </tbody>
          </table>
        </body>
        </html>
    """)


# ── Gate summary stats ────────────────────────────────────────────────────

def _gate_stats(replay: dict) -> dict:
    ticks  = replay["ticks"]
    ratios = [t["ratio"] for t in ticks if t["gate_state"] != "INACTIVE"]
    if not ratios:
        return {}

    # PASS -> not-PASS transitions
    n_ptf = 0
    total_pass_sec = 0.0
    in_pass = False
    pass_start = None
    for t in ticks:
        st = t["gate_state"]
        if st == "PASS" and not in_pass:
            in_pass     = True
            pass_start  = t["t_sec"]
        elif st != "PASS" and in_pass:
            in_pass = False
            n_ptf  += 1
            total_pass_sec += t["t_sec"] - pass_start
    if in_pass:
        total_pass_sec += ticks[-1]["t_sec"] - pass_start

    return {
        "mean_ratio":              round(float(np.mean(ratios)), 4),
        "min_ratio":               round(float(np.min(ratios)),  4),
        "n_pass_to_fail":          n_ptf,
        "total_pass_duration_sec": round(total_pass_sec, 1),
    }


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    for d in (REPLAY_DIR, OHLC_DIR, CHART_DIR):
        d.mkdir(parents=True, exist_ok=True)

    with open(SELECTED) as f:
        selected = json.load(f)

    hawkes_median, q_bar_cfg = _load_config()

    print(f"Phase DIAG-GATE: processing {len(selected)} events")
    print()

    all_t2b_ok = True
    summary_rows = []

    for ev in selected:
        slot   = ev["slot"]
        ticker = ev["ticker"]
        date   = ev["date"]
        label  = f"[{slot}] {ticker} {date}"
        print(f"{'=' * 60}")
        print(f"  {label}")

        # ── T2: Replay (skip if JSON already exists on disk) ──
        replay_path = REPLAY_DIR / f"{ticker}_{date}.json"
        if replay_path.exists():
            print(f"  T2  replay ... CACHED (loading {replay_path.name})", flush=True)
            with open(replay_path) as f:
                replay = json.load(f)
            print(f"       ({replay['n_ticks']:,} ticks loaded from disk)")
        else:
            print(f"  T2  replay ...", end=" ", flush=True)
            replay = _replay_event(ev, hawkes_median, q_bar_cfg)
            with open(replay_path, "w") as f:
                json.dump(replay, f, indent=2)
            print(f"OK  ({replay['n_ticks']:,} ticks -> {replay_path.name})")

        # ── T2b: entry lag check ──
        ok = _t2b_check(ev, replay)
        if not ok:
            all_t2b_ok = False

        # ── Gate summary stats ──
        stats = _gate_stats(replay)
        print(f"  Stats: mean_ratio={stats.get('mean_ratio')}  "
              f"min_ratio={stats.get('min_ratio')}  "
              f"pass_to_fail={stats.get('n_pass_to_fail')}  "
              f"total_pass_sec={stats.get('total_pass_duration_sec')}")

        # ── T3: 30s OHLC ──
        ohlc_path  = OHLC_DIR  / f"{ticker}_{date}.json"
        chart_path = CHART_DIR / f"{ticker}_{date}.html"
        if ohlc_path.exists() and chart_path.exists():
            print(f"  T3  OHLC 30s ... CACHED ({ohlc_path.name})")
            with open(ohlc_path) as f:
                ohlc = json.load(f)
        else:
            print(f"  T3  OHLC 30s ...", end=" ", flush=True)
            ohlc = _build_ohlc_30s(replay)
            with open(ohlc_path, "w") as f:
                json.dump(ohlc, f, indent=2)
            print(f"OK  ({ohlc['n_bars']} bars -> {ohlc_path.name})")

        # ── T4: Chart ──
        if chart_path.exists():
            print(f"  T4  chart ... CACHED ({chart_path.name})")
        else:
            print(f"  T4  chart ...", end=" ", flush=True)
            html = _make_chart(ev, replay, ohlc)
            with open(chart_path, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"OK  -> {chart_path.name}")

        summary_rows.append({**ev, **stats})
        print()

    # ── T5: index ──
    print("T5  writing index.html ...", end=" ", flush=True)
    index_html = _make_index(selected)
    index_path = CHART_DIR / "index.html"
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(index_html)
    print(f"OK -> {index_path}")

    # ── Final report ──
    print()
    print("=" * 70)
    print("DIAG-GATE summary")
    print("=" * 70)
    hdr = f"{'Slot':4s} {'Ticker':8s} {'Date':12s} {'Exit':20s} {'PnL%':8s} {'mean_r':7s} {'min_r':6s} {'PTF':4s} {'pass_s':8s}"
    print(hdr)
    print("-" * len(hdr))
    for r in summary_rows:
        print(
            f"{r['slot']:4s} {r['ticker']:8s} {r['date']:12s} "
            f"{r.get('exit_reason',''):20s} {r.get('pnl_pct', 0):+8.2f}  "
            f"{r.get('mean_ratio', 0):7.4f} {r.get('min_ratio', 0):6.4f}  "
            f"{r.get('n_pass_to_fail', 0):3d}  {r.get('total_pass_duration_sec', 0):8.1f}"
        )

    print()
    print(f"T2b all entries after scanner hit: {'PASS' if all_t2b_ok else 'FAIL'}")
    print()
    print("Output files:")
    print(f"  Replay data : {REPLAY_DIR}")
    print(f"  OHLC 30s    : {OHLC_DIR}")
    print(f"  Charts      : {CHART_DIR}")
    print(f"  Index       : {CHART_DIR / 'index.html'}")
    print()
    print("Done.")


if __name__ == "__main__":
    main()
