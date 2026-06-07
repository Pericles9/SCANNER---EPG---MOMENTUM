"""
T7 — Per-event 4-panel charts for Phase WJI-OPT val sample.

Runs the winning WJI config (from val_selected.json) over all val events,
generates a chart for every event with >= 1 trade.

Panel layout (RunningMaxGate, no slope, no EXIT_D/LULD):
  1 — 10s OHLCV candlesticks. Entry ▲ (green). Exit ▼ (green=win, red=loss).
      available_move high shading (light blue). Per-event capture_fraction in title.
  2 — WJI signal (blue). Running-max peak (grey). p×peak threshold (dashed green).
      PASS window shading (light green).
  3 — Components: norm_λ_V (blue), norm_λ_buy (orange). Reference at 1.0.
  4 — Gate state: PASS (green) / FAIL (red) / WARMUP (grey) horizontal bands.

Output:
  results/phase_wji_opt/event_charts/{TICKER}_{DATE}.html
  results/phase_wji_opt/event_charts/index.html  (sortable table)
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from data.loaders.trades import load_trades, _session_ns_bounds
from data.loaders.quotes import load_quotes
from data.schemas.mom_db import CONFIG_DIR, NS_PER_SECOND
from core.epg.gate import GateState
from core.epg.gate_variants import RunningMaxGate
from core.ofi.trade_ofi import compute_trade_ofi
from tools.sweep_runner_opt2 import precompute_sf_trajectory, sf_is_qualified_at
from tools.phase_wji_opt.common import (
    compute_wji_signal, compute_lambda_v_signal,
    _compute_lambda_v_ref, build_config_grid,
)

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

VAL_SAMPLE_PATH = REPO_ROOT / "results" / "phase_wji_poc" / "quality_sample_val.json"
VAL_CACHE_PATH = REPO_ROOT / "results" / "phase_wji_poc" / ".cache_val_results.json"
VAL_SELECTED_PATH = REPO_ROOT / "results" / "phase_wji_opt" / "val_selected.json"
OUT_DIR = REPO_ROOT / "results" / "phase_wji_opt" / "event_charts"
OHLC_BIN_SEC = 10.0
EPG_WARMUP = 300.0
TAU_V = 180.0
BETA_SLOW = 0.01
ALPHA = 0.50
LN2 = math.log(2)


# ══════════════════════════════════════════════════════════════════════
#  Gate replay with time-series capture
# ══════════════════════════════════════════════════════════════════════

def replay_for_chart(
    cfg: dict,
    signal_arr: np.ndarray,
    norm_v_arr: np.ndarray,
    norm_buy_arr: np.ndarray,
    t_sec,
    prices,
    t_event: float,
    sf,
    timestamps_ns,
) -> dict:
    """
    Replay RunningMaxGate while capturing per-tick arrays for all 4 chart panels.
    """
    gate = RunningMaxGate(
        p=cfg["p"], hysteresis=cfg["hysteresis"], p_close=cfg["p_close"],
        warmup_seconds=EPG_WARMUP,
    )
    gate.activate(t_event)

    N = len(t_sec)
    use_sf = sf is not None and sf.n_bars > 0
    prices_arr = np.asarray(prices, dtype=np.float64)
    max_from = np.maximum.accumulate(prices_arr[::-1])[::-1]

    peak_arr = np.zeros(N, dtype=np.float64)
    threshold_arr = np.zeros(N, dtype=np.float64)
    gate_state_arr: list[GateState] = []

    prev_state = GateState.INACTIVE
    in_position = False
    entry_t: Optional[float] = None
    entry_price: Optional[float] = None
    entry_idx: Optional[int] = None

    entries: list[dict] = []
    exits: list[dict] = []

    for i in range(N):
        t = float(t_sec[i])
        sig = float(signal_arr[i])

        state = gate.update(sig, t)
        peak_arr[i] = gate.peak
        threshold_arr[i] = cfg["p"] * gate.peak
        gate_state_arr.append(state)

        if not in_position:
            rising_edge = (
                state == GateState.PASS
                and prev_state in (GateState.INACTIVE, GateState.WARMUP, GateState.FAIL)
            )
            if rising_edge:
                sf_ok = True
                if use_sf:
                    sf_ok = sf_is_qualified_at(sf, int(timestamps_ns[i]))
                if sf_ok:
                    in_position = True
                    entry_t = t
                    entry_price = float(prices_arr[min(i + 1, N - 1)])
                    entry_idx = min(i + 1, N - 1)
                    entries.append({"t": t, "price": entry_price})
        else:
            if prev_state == GateState.PASS and state != GateState.PASS:
                exit_price = float(prices_arr[min(i + 1, N - 1)])
                pnl = (exit_price - entry_price) / entry_price * 100.0
                avail = max(float(max_from[entry_idx]) / entry_price - 1.0, 0.0) * 100.0
                exits.append({
                    "t": t, "price": exit_price,
                    "pnl": pnl, "available": avail,
                })
                in_position = False
                entry_t = None
                entry_price = None
                entry_idx = None

        prev_state = state

    if in_position:
        exit_price = float(prices_arr[N - 1])
        pnl = (exit_price - entry_price) / entry_price * 100.0
        avail = max(float(max_from[entry_idx]) / entry_price - 1.0, 0.0) * 100.0
        exits.append({
            "t": float(t_sec[N - 1]), "price": exit_price,
            "pnl": pnl, "available": avail,
        })

    # Per-event capture_fraction
    sum_pnl = sum(e["pnl"] for e in exits)
    sum_avail = sum(e["available"] for e in exits)
    capture_fraction = sum_pnl / sum_avail if sum_avail > 0 else None

    return {
        "t_arr": np.asarray(t_sec, dtype=np.float64),
        "prices_arr": prices_arr,
        "signal_arr": signal_arr,
        "peak_arr": peak_arr,
        "threshold_arr": threshold_arr,
        "norm_v_arr": norm_v_arr,
        "norm_buy_arr": norm_buy_arr,
        "gate_state_arr": gate_state_arr,
        "t_event": t_event,
        "t_warmup_end": t_event + EPG_WARMUP,
        "entries": entries,
        "exits": exits,
        "n_trades": len(exits),
        "total_pnl": sum_pnl,
        "capture_fraction": capture_fraction,
    }


# ══════════════════════════════════════════════════════════════════════
#  OHLCV aggregation
# ══════════════════════════════════════════════════════════════════════

def ticks_to_ohlc(t_sec: np.ndarray, prices: np.ndarray, bin_sec: float = OHLC_BIN_SEC):
    if len(t_sec) == 0:
        return [], [], [], [], []
    t0 = float(t_sec[0])
    bins = np.floor((t_sec - t0) / bin_sec).astype(int)
    bar_t, bar_o, bar_h, bar_l, bar_c = [], [], [], [], []
    for b in range(int(bins[-1]) + 1):
        mask = bins == b
        if not mask.any():
            continue
        p = prices[mask]
        ts = t_sec[mask]
        bar_t.append(float(ts[0]))
        bar_o.append(float(p[0]))
        bar_h.append(float(p.max()))
        bar_l.append(float(p.min()))
        bar_c.append(float(p[-1]))
    return bar_t, bar_o, bar_h, bar_l, bar_c


# ══════════════════════════════════════════════════════════════════════
#  Shading helpers
# ══════════════════════════════════════════════════════════════════════

def _add_state_shading(fig, t, states, target_state, fill_color, row):
    N = len(t)
    in_region = False
    region_start = None
    for i in range(N):
        if states[i] == target_state and not in_region:
            in_region = True
            region_start = float(t[i])
        elif states[i] != target_state and in_region:
            in_region = False
            fig.add_vrect(x0=region_start, x1=float(t[i]),
                          fillcolor=fill_color, layer="below", line_width=0, row=row, col=1)
    if in_region:
        fig.add_vrect(x0=region_start, x1=float(t[-1]),
                      fillcolor=fill_color, layer="below", line_width=0, row=row, col=1)


# ══════════════════════════════════════════════════════════════════════
#  Chart builder
# ══════════════════════════════════════════════════════════════════════

def build_chart(data: dict, ticker: str, date: str, mom_pct: float, cfg: dict) -> "go.Figure":
    t = data["t_arr"]
    prices = data["prices_arr"]
    signal = data["signal_arr"]
    peak = data["peak_arr"]
    threshold = data["threshold_arr"]
    norm_v = data["norm_v_arr"]
    norm_buy = data["norm_buy_arr"]
    states = data["gate_state_arr"]
    t_event = data["t_event"]
    t_warmup_end = data["t_warmup_end"]
    entries = data["entries"]
    exits = data["exits"]
    n_trades = data["n_trades"]
    total_pnl = data["total_pnl"]
    cf = data["capture_fraction"]
    cf_str = f"{cf:.3f}" if cf is not None else "N/A"

    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        row_heights=[0.35, 0.25, 0.20, 0.20],
        vertical_spacing=0.03,
        subplot_titles=[
            f"{ticker} {date} (+{mom_pct:.1f}%) | {n_trades} trades | PnL: {total_pnl:+.1f}% | capture: {cf_str}",
            f"WJI signal (p={cfg['p']}, {cfg['hysteresis']}, p_close={cfg['p_close']})",
            "Components: norm_λ_V (blue) vs norm_λ_buy (orange)",
            "Gate state",
        ],
    )

    # ── Panel 1: OHLCV ───────────────────────────────────────────────
    bar_t, bar_o, bar_h, bar_l, bar_c = ticks_to_ohlc(t, prices, OHLC_BIN_SEC)
    if bar_t:
        fig.add_trace(go.Candlestick(
            x=bar_t, open=bar_o, high=bar_h, low=bar_l, close=bar_c,
            name="Price", showlegend=False,
            increasing_line_color="#2ca02c", decreasing_line_color="#d62728",
        ), row=1, col=1)

    # available_move high shading (light blue): from entry to max price visible forward
    for en, ex in zip(entries, exits):
        # Shade from entry to the first bar at or after exit that represents max price
        t_mask = (t >= en["t"]) & (t <= ex["t"])
        if t_mask.any():
            max_p = float(prices[t_mask].max())
            fig.add_hrect(
                y0=en["price"], y1=max_p,
                x0=en["t"], x1=ex["t"],
                fillcolor="rgba(30,144,255,0.08)", line_width=0,
                row=1, col=1,
            )

    for en in entries:
        fig.add_trace(go.Scatter(
            x=[en["t"]], y=[en["price"]], mode="markers",
            marker=dict(symbol="triangle-up", size=10, color="green"),
            showlegend=False,
        ), row=1, col=1)
    for ex in exits:
        color = "green" if ex["pnl"] >= 0 else "red"
        fig.add_trace(go.Scatter(
            x=[ex["t"]], y=[ex["price"]], mode="markers",
            marker=dict(symbol="triangle-down", size=10, color=color),
            name=f"{ex['pnl']:+.1f}%", showlegend=False,
        ), row=1, col=1)

    fig.add_vline(x=t_event, line_dash="dash", line_color="orange",
                  annotation_text="T_event", annotation_position="top left", row=1, col=1)
    if t_warmup_end <= float(t[-1]):
        fig.add_vline(x=t_warmup_end, line_dash="dot", line_color="grey",
                      annotation_text="warmup_end", annotation_position="top left", row=1, col=1)

    # ── Panel 2: WJI signal ───────────────────────────────────────────
    # PASS shading
    _add_state_shading(fig, t, states, GateState.PASS, "rgba(0,180,0,0.15)", 2)
    _add_state_shading(fig, t, states, GateState.WARMUP, "rgba(180,180,180,0.15)", 2)

    fig.add_trace(go.Scatter(
        x=list(t), y=list(signal),
        mode="lines", line=dict(color="#1f77b4", width=1.5),
        name="WJI", showlegend=False,
    ), row=2, col=1)
    fig.add_trace(go.Scatter(
        x=list(t), y=list(peak),
        mode="lines", line=dict(color="lightgrey", width=1),
        name="Peak", showlegend=False,
    ), row=2, col=1)
    fig.add_trace(go.Scatter(
        x=list(t), y=list(threshold),
        mode="lines", line=dict(color="#2ca02c", width=1, dash="dash"),
        name=f"p×peak ({cfg['p']})", showlegend=False,
    ), row=2, col=1)

    # ── Panel 3: Components ───────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=list(t), y=list(norm_v),
        mode="lines", line=dict(color="#1f77b4", width=1),
        name="norm_λ_V", showlegend=False,
    ), row=3, col=1)
    fig.add_trace(go.Scatter(
        x=list(t), y=list(norm_buy),
        mode="lines", line=dict(color="#ff7f0e", width=1),
        name="norm_λ_buy", showlegend=False,
    ), row=3, col=1)
    fig.add_hline(y=1.0, line_dash="dot", line_color="grey",
                  annotation_text="background", annotation_position="right", row=3, col=1)

    # ── Panel 4: Gate state ───────────────────────────────────────────
    _add_state_shading(fig, t, states, GateState.PASS, "rgba(0,180,0,0.60)", 4)
    _add_state_shading(fig, t, states, GateState.FAIL, "rgba(214,39,40,0.40)", 4)
    _add_state_shading(fig, t, states, GateState.WARMUP, "rgba(180,180,180,0.50)", 4)
    fig.add_trace(go.Scatter(
        x=list(t), y=[0.5] * len(t),
        mode="lines", line=dict(color="rgba(0,0,0,0)", width=0),
        showlegend=False,
    ), row=4, col=1)

    # ── Layout ────────────────────────────────────────────────────────
    fig.update_layout(
        height=960,
        template="plotly_white",
        title=f"{ticker} {date} | WJI-OPT val | config: {cfg['config_id']}",
        hovermode="x unified",
        margin=dict(l=60, r=20, t=80, b=40),
        xaxis_rangeslider_visible=False,
    )
    fig.update_yaxes(title_text="Price ($)", row=1, col=1)
    fig.update_yaxes(title_text="WJI", row=2, col=1)
    fig.update_yaxes(title_text="Norm intensity", row=3, col=1)
    fig.update_yaxes(title_text="Gate state", row=4, col=1,
                     tickvals=[], range=[0, 1])
    fig.update_xaxes(title_text="Seconds since session start", row=4, col=1)

    return fig


# ══════════════════════════════════════════════════════════════════════
#  Index HTML
# ══════════════════════════════════════════════════════════════════════

def build_index_html(chart_files: list[dict]) -> str:
    rows = ""
    for cf_row in sorted(chart_files, key=lambda x: (x["date"], x["ticker"])):
        pnl_str = f"{cf_row['total_pnl']:+.1f}%" if cf_row["total_pnl"] is not None else "—"
        cf_str = f"{cf_row['capture_fraction']:.3f}" if cf_row["capture_fraction"] is not None else "—"
        ml_str = f"{cf_row['max_loss_pct']:.2f}%" if cf_row["max_loss_pct"] is not None else "—"
        rows += (
            f"<tr>"
            f"<td>{cf_row['ticker']}</td>"
            f"<td>{cf_row['date']}</td>"
            f"<td>{cf_row['n_trades']}</td>"
            f"<td>{pnl_str}</td>"
            f"<td>{cf_str}</td>"
            f"<td>{ml_str}</td>"
            f"<td><a href='{cf_row['filename']}'>chart</a></td>"
            f"</tr>\n"
        )

    return f"""<!DOCTYPE html>
<html><head>
<title>Phase WJI-OPT — Val Charts</title>
<style>
  body {{ font-family: monospace; padding: 20px; }}
  table {{ border-collapse: collapse; }}
  td, th {{ border: 1px solid #ccc; padding: 4px 8px; }}
  th {{ cursor: pointer; background: #f0f0f0; user-select: none; }}
  tr:hover {{ background: #f8f8ff; }}
</style>
</head><body>
<h2>Phase WJI-OPT — Val Sample Charts</h2>
<p>{len(chart_files)} events with &ge;1 trade</p>
<table id="t">
<thead><tr>
  <th onclick="sortTable(0)">Ticker</th>
  <th onclick="sortTable(1)">Date</th>
  <th onclick="sortTable(2)">n_trades</th>
  <th onclick="sortTable(3)">Total PnL</th>
  <th onclick="sortTable(4)">Capture Fraction</th>
  <th onclick="sortTable(5)">Max Loss</th>
  <th>Chart</th>
</tr></thead>
<tbody>
{rows}
</tbody>
</table>
<script>
function sortTable(col) {{
  var t = document.getElementById("t"), rows = Array.from(t.tBodies[0].rows);
  rows.sort((a, b) => a.cells[col].innerText.localeCompare(b.cells[col].innerText, undefined, {{numeric: true}}));
  t.tBodies[0].append(...rows);
}}
</script>
</body></html>"""


# ══════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════

def main() -> None:
    if not HAS_PLOTLY:
        log.error("plotly not installed. Run: pip install plotly")
        sys.exit(1)

    for p in [VAL_SAMPLE_PATH, VAL_CACHE_PATH, VAL_SELECTED_PATH]:
        if not p.exists():
            log.error("Required file not found: %s", p)
            sys.exit(1)

    with open(VAL_SELECTED_PATH) as f:
        val_selected = json.load(f)
    meta = val_selected.get("meta", {})
    wji_winner_id = meta.get("config_id")
    if not wji_winner_id:
        log.error("No config_id in val_selected.json meta.")
        sys.exit(1)

    grid = build_config_grid()
    grid_by_id = {c["config_id"]: c for c in grid}
    cfg = grid_by_id[wji_winner_id]
    log.info("Charting config: %s (p=%.2f, %s)", wji_winner_id, cfg["p"], cfg["hysteresis"])

    with open(VAL_SAMPLE_PATH) as f:
        val_sample = json.load(f)
    with open(VAL_CACHE_PATH) as f:
        raw_cache = json.load(f)

    cache_index = {(r["ticker"], r["date"]): r for r in raw_cache if r.get("status") == "ok"}

    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    n_charts = 0
    n_no_trades = 0
    n_fail = 0
    chart_index: list[dict] = []
    t0 = time.time()

    val_events = val_sample["events"]
    log.info("Processing %d val events...", len(val_events))

    for i, ev in enumerate(val_events):
        ticker, date, mom_pct = ev["ticker"], ev["date"], ev["mom_pct"]
        key_str = f"{ticker}_{date}"
        log.info("[%d/%d] %s", i + 1, len(val_events), key_str)

        cached = cache_index.get((ticker, date))
        if cached is None:
            log.warning("  %s: not in val cache, skipping", key_str)
            n_fail += 1
            continue

        t_event = cached["t_event"]
        mu_buy = cached.get("mu_buy", 0.01)

        try:
            td = load_trades(ticker, date, mom_pct)
            if td.n_trades < 30:
                log.info("  %s: too few trades (%d), skipping", key_str, td.n_trades)
                n_fail += 1
                continue

            qd = load_quotes(ticker, date, mom_pct)
            if qd is None or qd.n_quotes < 10:
                log.info("  %s: insufficient quotes, skipping", key_str)
                n_fail += 1
                continue

            tier_qbar = q_bar_cfg.get("wide", {}).get("median", 250.0)
            ofi_result = compute_trade_ofi(
                trade_timestamps=td.timestamps, trade_prices=td.prices,
                trade_sizes=td.sizes.astype(np.float64),
                quote_timestamps=qd.timestamps,
                quote_bid_prices=qd.bid_prices, quote_ask_prices=qd.ask_prices,
                quote_bid_sizes=qd.bid_sizes.astype(np.float64),
                quote_ask_sizes=qd.ask_sizes.astype(np.float64),
                window_sec=10.0, q_bar_fallback=tier_qbar,
            )
            sides = ofi_result.sides

            start_ns, end_ns = _session_ns_bounds(date)
            sf = precompute_sf_trajectory(td, start_ns, end_ns)

            # Compute WJI signal and component arrays
            wji_arr, lv_ref = compute_wji_signal(
                td.prices, td.sizes, td.t_sec, sides, t_event,
                mu_buy, tau_v=TAU_V, beta_slow=BETA_SLOW, alpha=ALPHA,
            )
            lv_arr, _ = compute_lambda_v_signal(td.prices, td.sizes, td.t_sec, t_event, tau_v=TAU_V)

            # norm_λ_buy: compute buy-side EMA / mu_buy
            norm_buy_arr = _compute_norm_buy_arr(sides, td.t_sec, mu_buy)

            chart_data = replay_for_chart(
                cfg, wji_arr, lv_arr, norm_buy_arr,
                td.t_sec, td.prices, t_event, sf, td.timestamps,
            )

            if chart_data["n_trades"] == 0:
                n_no_trades += 1
                continue

            fname = f"{ticker}_{date}.html"
            fig = build_chart(chart_data, ticker, date, mom_pct, cfg)
            fig.write_html(str(OUT_DIR / fname), include_plotlyjs="cdn", full_html=True)
            n_charts += 1
            chart_index.append({
                "ticker": ticker,
                "date": date,
                "filename": fname,
                "n_trades": chart_data["n_trades"],
                "total_pnl": chart_data["total_pnl"],
                "capture_fraction": chart_data["capture_fraction"],
                "max_loss_pct": min((e["pnl"] for e in chart_data["exits"]), default=None),
            })
            log.info("  %s: %d trades, PnL=%.1f%%, CF=%.3f",
                     key_str, chart_data["n_trades"], chart_data["total_pnl"],
                     chart_data["capture_fraction"] or 0.0)

        except Exception as e:
            import traceback
            log.error("  %s: failed: %s\n%s", key_str, e, traceback.format_exc())
            n_fail += 1

    # Write index
    index_html = build_index_html(chart_index)
    idx_path = OUT_DIR / "index.html"
    idx_path.write_text(index_html, encoding="utf-8")

    log.info(
        "\nT7 complete: %d charts, %d no-trades, %d failures in %.1fs",
        n_charts, n_no_trades, n_fail, time.time() - t0,
    )
    log.info("Index: %s", idx_path)


def _compute_norm_buy_arr(sides: np.ndarray, t_sec, mu_buy: float) -> np.ndarray:
    """Compute per-tick norm_λ_buy = λ_buy_EMA / mu_buy (using BETA_SLOW decay)."""
    EPS = 1e-9
    N = len(t_sec)
    mu_buy_safe = max(float(mu_buy), EPS)
    lb = 0.0
    last_t: Optional[float] = None
    arr = np.empty(N, dtype=np.float64)
    for i in range(N):
        t = float(t_sec[i])
        if last_t is not None:
            dt = max(0.0, t - last_t)
            lb *= math.exp(-BETA_SLOW * dt)
        if int(sides[i]) == 1:
            lb += BETA_SLOW
        last_t = t
        arr[i] = lb / mu_buy_safe
    return arr


if __name__ == "__main__":
    main()
