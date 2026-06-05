"""
T5 — 4-panel charts for all val events with >= 1 WJI trade.

Panel layout:
  1 — Price: 10s OHLCV candlesticks. PASS shading (green). Entry (▲) / exit (▼).
              SF non-qualified periods shaded light red.
  2 — WJI signal: WJI(t), p_open×peak (dashed), p_close×peak (dotted), peak (thin grey).
              Slope-decay periods shaded light orange.
  3 — Component decomposition: norm_λ_V (blue), norm_λ_buy (orange). Reference at 1.0.
  4 — Slope regime: slope_WJI bar chart. Green = positive, red = negative. Zero line.

Output:
  results/phase_wji_poc/charts/{TICKER}_{DATE}.html
  results/phase_wji_poc/charts/index.html  (sortable table)
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

from data.loaders.trades import load_trades, compute_lambda_ref_per_event, _session_ns_bounds
from data.loaders.quotes import load_quotes
from data.schemas.mom_db import CONFIG_DIR, NS_PER_SECOND
from core.epg.anchor import EventAnchor
from core.epg.gate import GateState
from core.epg.gate_variants import WJIGate
from core.ofi.trade_ofi import compute_trade_ofi
from tools.t3_sweep_runner import _hawkes_replay_with_refit, EPG_K, EPG_WARMUP
from tools.sweep_runner_opt2 import precompute_sf_trajectory, sf_is_qualified_at
from tools.phase_wji_poc.common import _compute_lambda_v_ref, get_q_tilde_at_t_event

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

OUT_DIR = REPO_ROOT / "results" / "phase_wji_poc" / "charts"
WJI_CONFIG_PATH = REPO_ROOT / "config" / "phase_wji_poc" / "wji_poc.json"
OHLC_BIN_SEC = 10.0


def load_wji_config() -> dict:
    with open(WJI_CONFIG_PATH) as f:
        raw = json.load(f)
    wp = raw["wji"]
    return {
        "config_id": raw["config_id"],
        "alpha": wp["alpha"],
        "tau_v": float(wp["tau_v"]),
        "beta_slow": wp["beta_slow"],
        "L_sec": float(wp["L_sec"]),
        "tau_decay": float(wp["tau_decay"]),
        "p_open": wp["p_open"],
        "p_close": wp["p_close"],
    }


def replay_for_chart(
    cfg: dict,
    td,
    sides: np.ndarray,
    t_event: float,
    mu_buy: float,
    sf,
) -> Optional[dict]:
    """
    Full gate replay recording per-tick arrays for all 4 chart panels.
    Returns None if no trades were produced.
    """
    lambda_v_ref = _compute_lambda_v_ref(td, t_event, cfg["tau_v"])

    gate = WJIGate(
        alpha=cfg["alpha"], tau_v=cfg["tau_v"], beta_slow=cfg["beta_slow"],
        L_sec=cfg["L_sec"], tau_decay=cfg["tau_decay"],
        p_open=cfg["p_open"], p_close=cfg["p_close"],
        warmup_seconds=EPG_WARMUP,
    )
    gate.activate(t_event, lambda_v_ref, max(float(mu_buy), 1e-9))

    use_sf = sf is not None and sf.n_bars > 0
    N = td.n_trades

    # Per-tick arrays
    t_arr = td.t_sec
    price_arr = np.array(td.prices, dtype=float)
    wji_arr = np.zeros(N)
    peak_arr = np.zeros(N)
    p_open_thr_arr = np.zeros(N)
    p_close_thr_arr = np.zeros(N)
    norm_v_arr = np.zeros(N)
    norm_buy_arr = np.zeros(N)
    slope_arr = np.zeros(N)
    gate_state_arr = []
    sf_qualified_arr = np.zeros(N, dtype=bool)

    prev_state = GateState.INACTIVE
    in_position = False
    entry_t_sec: Optional[float] = None
    entry_price: Optional[float] = None

    entries: list[dict] = []
    exits: list[dict] = []

    for i in range(N):
        dv = float(td.prices[i]) * float(td.sizes[i])
        t = td.t_sec[i]

        state = gate.update(dv, t, int(sides[i]))
        gate_state_arr.append(state)

        wji_arr[i] = gate.wji
        peak_arr[i] = gate.peak
        p_open_thr_arr[i] = cfg["p_open"] * gate.peak
        p_close_thr_arr[i] = cfg["p_close"] * gate.peak
        norm_v_arr[i] = gate.norm_lambda_v
        norm_buy_arr[i] = gate.norm_lambda_buy
        slope_arr[i] = gate.slope_wji

        if use_sf:
            sf_qualified_arr[i] = sf_is_qualified_at(sf, int(td.timestamps[i]))
        else:
            sf_qualified_arr[i] = True

        if not in_position:
            rising_edge = (
                state == GateState.PASS
                and prev_state in (GateState.INACTIVE, GateState.WARMUP, GateState.FAIL)
            )
            if rising_edge:
                sf_ok = True if not use_sf else sf_is_qualified_at(sf, int(td.timestamps[i]))
                if sf_ok:
                    entry_t_sec = t
                    entry_price = float(td.prices[min(i + 1, N - 1)])
                    in_position = True
                    entries.append({"t": t, "price": entry_price})
        else:
            if prev_state == GateState.PASS and state != GateState.PASS:
                exit_price = float(td.prices[min(i + 1, N - 1)])
                pnl = (exit_price - entry_price) / entry_price * 100.0
                exits.append({"t": t, "price": exit_price, "pnl": pnl})
                in_position = False
                entry_t_sec = None
                entry_price = None

        prev_state = state

    if in_position:
        exit_price = float(td.prices[N - 1])
        pnl = (exit_price - entry_price) / entry_price * 100.0
        exits.append({"t": td.t_sec[N - 1], "price": exit_price, "pnl": pnl})

    n_trades = len(exits)
    total_pnl = round(sum(e["pnl"] for e in exits), 2)

    return {
        "t_arr": t_arr,
        "price_arr": price_arr,
        "wji_arr": wji_arr,
        "peak_arr": peak_arr,
        "p_open_thr_arr": p_open_thr_arr,
        "p_close_thr_arr": p_close_thr_arr,
        "norm_v_arr": norm_v_arr,
        "norm_buy_arr": norm_buy_arr,
        "slope_arr": slope_arr,
        "gate_state_arr": gate_state_arr,
        "sf_qualified_arr": sf_qualified_arr,
        "t_event": t_event,
        "t_warmup_end": t_event + EPG_WARMUP,
        "entries": entries,
        "exits": exits,
        "n_trades": n_trades,
        "total_pnl": total_pnl,
    }


def ticks_to_ohlc(
    t_sec: np.ndarray, prices: np.ndarray, bin_sec: float = OHLC_BIN_SEC
):
    """Aggregate tick data to OHLC bars."""
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


def build_chart(data: dict, ticker: str, date: str, mom_pct: float, cfg: dict) -> "go.Figure":
    t = data["t_arr"]
    prices = data["price_arr"]
    wji = data["wji_arr"]
    peak = data["peak_arr"]
    p_open_thr = data["p_open_thr_arr"]
    p_close_thr = data["p_close_thr_arr"]
    norm_v = data["norm_v_arr"]
    norm_buy = data["norm_buy_arr"]
    slope = data["slope_arr"]
    states = data["gate_state_arr"]
    sf_qual = data["sf_qualified_arr"]
    t_event = data["t_event"]
    t_warmup_end = data["t_warmup_end"]
    entries = data["entries"]
    exits = data["exits"]
    N = len(t)

    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        row_heights=[0.35, 0.25, 0.20, 0.20],
        vertical_spacing=0.03,
        subplot_titles=[
            f"{ticker} {date} (+{mom_pct:.1f}%) — WJI POC",
            f"WJI signal (α={cfg['alpha']}, τ_V={cfg['tau_v']:.0f}s, β={cfg['beta_slow']}, L={cfg['L_sec']:.0f}s)",
            "Component decomposition (norm_λ_V vs norm_λ_buy)",
            "WJI slope regime",
        ],
    )

    # ── Panel 1: Price (candlestick) ──────────────────────────────────
    bar_t, bar_o, bar_h, bar_l, bar_c = ticks_to_ohlc(t, prices, OHLC_BIN_SEC)
    if bar_t:
        fig.add_trace(go.Candlestick(
            x=bar_t, open=bar_o, high=bar_h, low=bar_l, close=bar_c,
            name="Price", showlegend=False,
            increasing_line_color="#2ca02c", decreasing_line_color="#d62728",
        ), row=1, col=1)

    # PASS shading — green
    _add_pass_shading(fig, t, states, "rgba(0,180,0,0.20)", 1)

    # SF non-qualified shading — light red
    _add_bool_shading(fig, t, ~sf_qual, "rgba(220,50,50,0.10)", 1)

    # Entry / exit markers
    for en in entries:
        fig.add_trace(go.Scatter(
            x=[en["t"]], y=[en["price"]], mode="markers",
            marker=dict(symbol="triangle-up", size=10, color="green"),
            name="Entry", showlegend=False,
        ), row=1, col=1)
    for ex in exits:
        color = "green" if ex["pnl"] > 0 else "red"
        fig.add_trace(go.Scatter(
            x=[ex["t"]], y=[ex["price"]], mode="markers",
            marker=dict(symbol="triangle-down", size=10, color=color),
            name=f"Exit ({ex['pnl']:.1f}%)", showlegend=False,
        ), row=1, col=1)

    fig.add_vline(x=t_event, line_dash="dash", line_color="orange",
                  annotation_text="T_event", annotation_position="top left", row=1, col=1)
    if t_warmup_end <= float(t[-1]):
        fig.add_vline(x=t_warmup_end, line_dash="dot", line_color="grey",
                      annotation_text="warmup", annotation_position="top left", row=1, col=1)

    # ── Panel 2: WJI signal ───────────────────────────────────────────
    # Slope-decay shading (orange): FAIL + slope < 0
    decay_periods = np.array([
        (s != GateState.PASS and sl < 0)
        for s, sl in zip(states, slope)
    ])
    _add_bool_shading(fig, t, decay_periods, "rgba(255,165,0,0.10)", 2)

    fig.add_trace(go.Scatter(
        x=list(t), y=list(wji),
        mode="lines", line=dict(color="#1f77b4", width=1.5),
        name="WJI", showlegend=False,
    ), row=2, col=1)

    fig.add_trace(go.Scatter(
        x=list(t), y=list(p_open_thr),
        mode="lines", line=dict(color="#d62728", width=1, dash="dash"),
        name=f"p_open×peak ({cfg['p_open']})", showlegend=False,
    ), row=2, col=1)

    fig.add_trace(go.Scatter(
        x=list(t), y=list(p_close_thr),
        mode="lines", line=dict(color="#ff7f0e", width=1, dash="dot"),
        name=f"p_close×peak ({cfg['p_close']})", showlegend=False,
    ), row=2, col=1)

    fig.add_trace(go.Scatter(
        x=list(t), y=list(peak),
        mode="lines", line=dict(color="lightgrey", width=1),
        name="Peak", showlegend=False,
    ), row=2, col=1)

    _add_pass_shading(fig, t, states, "rgba(0,180,0,0.12)", 2)

    # ── Panel 3: Component decomposition ─────────────────────────────
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

    # Reference at 1.0
    fig.add_hline(y=1.0, line_dash="dot", line_color="grey",
                  annotation_text="background", annotation_position="right", row=3, col=1)

    # ── Panel 4: Slope regime (bar chart) ─────────────────────────────
    colors = ["#2ca02c" if s >= 0 else "#d62728" for s in slope]
    fig.add_trace(go.Bar(
        x=list(t), y=list(slope),
        marker_color=colors, name="slope_WJI", showlegend=False,
    ), row=4, col=1)
    fig.add_hline(y=0, line_color="black", line_width=0.5, row=4, col=1)

    # ── Layout ────────────────────────────────────────────────────────
    n_trades = data["n_trades"]
    total_pnl = data["total_pnl"]
    fig.update_layout(
        height=900,
        template="plotly_white",
        title=f"{ticker} {date} | WJI POC | {n_trades} trades | total PnL: {total_pnl:+.1f}%",
        hovermode="x unified",
        margin=dict(l=60, r=20, t=60, b=40),
        xaxis_rangeslider_visible=False,
    )
    fig.update_yaxes(title_text="Price ($)", row=1, col=1)
    fig.update_yaxes(title_text="WJI", row=2, col=1)
    fig.update_yaxes(title_text="Norm intensity", row=3, col=1)
    fig.update_yaxes(title_text="Slope (WJI)", row=4, col=1)
    fig.update_xaxes(title_text="Seconds since session start", row=4, col=1)

    return fig


def _add_pass_shading(fig, t, states, fill_color, row):
    """Add green vrects for PASS windows."""
    N = len(t)
    in_pass = False
    pass_start = None
    for i in range(N):
        s = states[i]
        if s == GateState.PASS and not in_pass:
            in_pass = True
            pass_start = float(t[i])
        elif s != GateState.PASS and in_pass:
            in_pass = False
            fig.add_vrect(
                x0=pass_start, x1=float(t[i]),
                fillcolor=fill_color, layer="below", line_width=0,
                row=row, col=1,
            )
    if in_pass:
        fig.add_vrect(
            x0=pass_start, x1=float(t[-1]),
            fillcolor=fill_color, layer="below", line_width=0,
            row=row, col=1,
        )


def _add_bool_shading(fig, t, mask, fill_color, row):
    """Shade periods where boolean mask is True."""
    N = len(t)
    in_region = False
    region_start = None
    for i in range(N):
        if mask[i] and not in_region:
            in_region = True
            region_start = float(t[i])
        elif not mask[i] and in_region:
            in_region = False
            fig.add_vrect(
                x0=region_start, x1=float(t[i]),
                fillcolor=fill_color, layer="below", line_width=0,
                row=row, col=1,
            )
    if in_region:
        fig.add_vrect(
            x0=region_start, x1=float(t[-1]),
            fillcolor=fill_color, layer="below", line_width=0,
            row=row, col=1,
        )


def preprocess_event(ev: dict, hawkes_params: dict, q_bar_cfg: dict) -> Optional[dict]:
    """Hawkes pipeline + SF trajectory for one event."""
    ticker, date, mom_pct = ev["ticker"], ev["date"], ev["mom_pct"]
    try:
        td = load_trades(ticker, date, mom_pct)
        if td.n_trades < 30:
            return None
        qd = load_quotes(ticker, date, mom_pct)
        if qd is None or qd.n_quotes < 10:
            return None

        N = td.n_trades
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

        lam_buy_out = np.zeros(N, dtype=np.float64)
        lam_sell_out = np.zeros(N, dtype=np.float64)
        E_out = np.zeros(N, dtype=np.float64)
        Edot_out = np.zeros(N, dtype=np.float64)
        n_base_out = np.zeros(N, dtype=np.float64)

        global_lref = hawkes_params["mu_buy"] + hawkes_params["mu_sell"]
        per_event_lref = compute_lambda_ref_per_event(ticker, date)
        lambda_ref = (
            per_event_lref
            if not math.isnan(per_event_lref) and per_event_lref > 0
            else global_lref
        )
        rho = hawkes_params.get("rho", 0.99)

        cold_start_params = _hawkes_replay_with_refit(
            t_sec=td.t_sec, sides=sides,
            rho=rho, lambda_ref=lambda_ref, init_params=hawkes_params, rho_E=rho,
            lam_buy_out=lam_buy_out, lam_sell_out=lam_sell_out,
            E_out=E_out, Edot_out=Edot_out, n_base_out=n_base_out,
        )
        lambda_hat = lam_buy_out + lam_sell_out

        anchor_lref = hawkes_params["mu_buy"] + hawkes_params["mu_sell"]
        anchor = EventAnchor(lambda_ref=anchor_lref, k_multiplier=EPG_K)
        if cold_start_params is not None:
            lref_epg = cold_start_params.mu_buy + cold_start_params.mu_sell
            if lref_epg > 0:
                anchor.set_lambda_ref(lref_epg)

        t_event = None
        for i in range(N):
            t_ev = anchor.update(lambda_hat[i], td.t_sec[i])
            if t_ev is not None:
                t_event = t_ev
                break
        if t_event is None:
            return None

        start_ns, end_ns = _session_ns_bounds(date)
        sf = precompute_sf_trajectory(td, start_ns, end_ns)
        mu_buy = cold_start_params.mu_buy if cold_start_params is not None else hawkes_params["mu_buy"]

        return {
            "td": td, "sides": sides, "t_event": t_event,
            "mu_buy": float(mu_buy), "sf": sf,
        }
    except Exception as e:
        log.warning("%s %s: preprocess failed: %s", ticker, date, e)
        return None


def build_index_html(chart_files: list[dict]) -> str:
    """Build a sortable HTML index of all charts."""
    rows = ""
    for cf in sorted(chart_files, key=lambda x: (x["date"], x["ticker"])):
        pnl_str = f"{cf['pnl']:+.1f}%" if cf["pnl"] is not None else "—"
        rows += (
            f"<tr>"
            f"<td>{cf['ticker']}</td>"
            f"<td>{cf['date']}</td>"
            f"<td>{cf['n_trades']}</td>"
            f"<td>{pnl_str}</td>"
            f"<td><a href='{cf['filename']}'>chart</a></td>"
            f"</tr>\n"
        )

    return f"""<!DOCTYPE html>
<html><head>
<title>Phase WJI-POC — Val Charts</title>
<style>
  body {{ font-family: monospace; padding: 20px; }}
  table {{ border-collapse: collapse; }}
  td, th {{ border: 1px solid #ccc; padding: 4px 8px; }}
  th {{ cursor: pointer; background: #f0f0f0; }}
</style>
</head><body>
<h2>Phase WJI-POC — Val Sample Charts</h2>
<p>{len(chart_files)} events with &ge;1 WJI trade</p>
<table id="t">
<thead><tr>
  <th onclick="sortTable(0)">Ticker</th>
  <th onclick="sortTable(1)">Date</th>
  <th onclick="sortTable(2)">n_trades</th>
  <th onclick="sortTable(3)">Total PnL</th>
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


def main() -> None:
    if not HAS_PLOTLY:
        log.error("plotly not installed. Run: pip install plotly")
        sys.exit(1)

    val_sample_path = REPO_ROOT / "results" / "phase_wji_poc" / "quality_sample_val.json"
    if not val_sample_path.exists():
        log.error("quality_sample_val.json not found — run T2 first")
        sys.exit(1)

    with open(val_sample_path) as f:
        val_data = json.load(f)
    val_events = val_data["events"]
    log.info("Val sample: %d events", len(val_events))

    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_params = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    cfg = load_wji_config()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    n_charts = 0
    n_fail = 0
    n_no_trades = 0
    chart_index: list[dict] = []
    t0 = time.time()

    for i, ev in enumerate(val_events):
        ticker, date, mom_pct = ev["ticker"], ev["date"], ev["mom_pct"]
        key = f"{ticker}_{date}"
        log.info("[%d/%d] %s", i + 1, len(val_events), key)

        pre = preprocess_event(ev, hawkes_params, q_bar_cfg)
        if pre is None:
            log.warning("  %s: preprocessing failed", key)
            n_fail += 1
            continue

        try:
            chart_data = replay_for_chart(
                cfg, pre["td"], pre["sides"], pre["t_event"],
                pre["mu_buy"], pre["sf"],
            )
        except Exception as e:
            log.error("  %s: replay failed: %s", key, e)
            n_fail += 1
            continue

        if chart_data is None or chart_data["n_trades"] == 0:
            n_no_trades += 1
            continue

        fname = f"{ticker}_{date}.html"
        try:
            fig = build_chart(chart_data, ticker, date, mom_pct, cfg)
            fig.write_html(
                str(OUT_DIR / fname),
                include_plotlyjs="cdn",
                full_html=True,
            )
            n_charts += 1
            chart_index.append({
                "ticker": ticker,
                "date": date,
                "filename": fname,
                "n_trades": chart_data["n_trades"],
                "pnl": chart_data["total_pnl"],
            })
        except Exception as e:
            log.error("  %s: chart write failed: %s", key, e)
            n_fail += 1

    # Write index
    index_html = build_index_html(chart_index)
    (OUT_DIR / "index.html").write_text(index_html, encoding="utf-8")

    fail_rate = n_fail / len(val_events) if val_events else 0.0
    log.info(
        "\nT5 complete: %d charts, %d no-trades, %d failures (%.1f%%) in %.1fs",
        n_charts, n_no_trades, n_fail, fail_rate * 100, time.time() - t0,
    )
    log.info("Index: %s", OUT_DIR / "index.html")

    if fail_rate > 0.3:
        log.warning("WARNING: failure rate %.1f%% > 30%% — investigate", fail_rate * 100)


if __name__ == "__main__":
    main()
