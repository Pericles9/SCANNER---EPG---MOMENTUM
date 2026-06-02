"""
T7 — Generate 2-panel Plotly charts for Phase EPG-OPT2.

Chart sets:
  - All 84 Stage 1 configs × 20 events          = 1,680 charts
  - All 21 t120 cooling combos × 20 events       =   420 charts
  - Top 3 cooling per other 15 Stage 2 bases     =   900 charts
  - Top 20 Stage 3 configs × 20 events           =   400 charts
  Total: ~3,400 charts

Panel format by gate type:
  Level gate (A, no cooling): λ_V + p_open×peak dashed + p_close×peak dotted
  Level gate (A, w/ cooling): same + orange shading during cooling-active periods
  F_ss: norm_slope + k_open + k_close + grey dead band
  F_sl: two sub-panels (slope+k_open | λ_V+p_close×peak)

Writes:
  results/phase_epg_opt2/charts/{config_id}/{TICKER}_{DATE}.html
  results/phase_epg_opt2/charts/{config_id}/index.html
  results/phase_epg_opt2/charts/master_index.html
"""
from __future__ import annotations

import json
import logging
import math
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from data.loaders.trades import load_trades, compute_lambda_ref_per_event
from data.loaders.quotes import load_quotes
from data.schemas.mom_db import CONFIG_DIR
from core.epg.anchor import EventAnchor
from core.epg.gate import ParticipationGate, GateState
from core.epg.gate_variants import SlopeGate
from core.ofi.trade_ofi import compute_trade_ofi
from tools.t3_sweep_runner import _hawkes_replay_with_refit, EPG_K, EPG_WARMUP

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

CHART_EVENTS_PATH = REPO_ROOT / "results" / "phase_epg_opt2" / "chart_events.json"
STAGE1_RANKED = REPO_ROOT / "results" / "phase_epg_opt2" / "sweep" / "stage1_ranked.json"
STAGE2_RANKED = REPO_ROOT / "results" / "phase_epg_opt2" / "sweep" / "stage2_ranked.json"
STAGE2_BASE_CONFIGS = REPO_ROOT / "results" / "phase_epg_opt2" / "sweep" / "stage2_base_configs.json"
STAGE3_ALL_RANKED = REPO_ROOT / "results" / "phase_epg_opt2" / "sweep" / "stage3_all_ranked.json"
OUT_DIR = REPO_ROOT / "results" / "phase_epg_opt2" / "charts"
T120_BASE_ID = "s1_t120_po65_pc65"
CHART_FAILURE_THRESHOLD = 0.10  # 10% escalation threshold


# ══════════════════════════════════════════════════════════════════════
#  Config selection for charting
# ══════════════════════════════════════════════════════════════════════

def load_chart_configs() -> list[dict]:
    """Load all configs that need charts."""
    configs_by_id: dict[str, dict] = {}

    # All 84 Stage 1 configs
    if STAGE1_RANKED.exists():
        for r in json.load(open(STAGE1_RANKED)):
            configs_by_id[r["config_id"]] = r

    if STAGE2_RANKED.exists() and STAGE2_BASE_CONFIGS.exists():
        base_cfg = json.load(open(STAGE2_BASE_CONFIGS))
        base_ids = base_cfg.get("base_config_ids", [])
        all_s2 = json.load(open(STAGE2_RANKED))

        # All 21 t120 cooling combos
        for r in all_s2:
            if r.get("base_config_id") == T120_BASE_ID:
                configs_by_id[r["config_id"]] = r

        # Top 3 cooling combos per other 15 Stage 2 bases
        other_bases = [bid for bid in base_ids if bid != T120_BASE_ID]
        for base_id in other_bases:
            base_rows = sorted(
                [r for r in all_s2 if r.get("base_config_id") == base_id],
                key=lambda x: x.get("borda_score") or 9999,
            )
            for r in base_rows[:3]:
                configs_by_id[r["config_id"]] = r

    # Top 20 Stage 3 configs (combined F_ss + F_sl)
    if STAGE3_ALL_RANKED.exists():
        all_s3 = json.load(open(STAGE3_ALL_RANKED))
        for r in all_s3[:20]:
            configs_by_id[r["config_id"]] = r

    return list(configs_by_id.values())


# ══════════════════════════════════════════════════════════════════════
#  Event preprocessing (Hawkes MLE + T_event)
# ══════════════════════════════════════════════════════════════════════

def preprocess_event(ev: dict, hawkes_params: dict, q_bar_cfg: dict) -> Optional[dict]:
    """
    Full preprocessing for one chart event.
    Returns dict with td, sides, t_event, cold_start_params, lv_ref.
    """
    ticker, date = ev["ticker"], ev["date"]
    mom_pct = ev.get("mom_pct", 0.3)
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
        lambda_ref = (per_event_lref if not math.isnan(per_event_lref) and per_event_lref > 0
                      else global_lref)

        cold_start_params = _hawkes_replay_with_refit(
            t_sec=td.t_sec, sides=sides, rho=hawkes_params.get("rho", 0.99),
            lambda_ref=lambda_ref, init_params=hawkes_params, rho_E=hawkes_params.get("rho", 0.99),
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
            log.warning("%s %s: no T_event detected", ticker, date)
            return None

        lv_ref = (cold_start_params.mu_buy + cold_start_params.mu_sell
                  if cold_start_params is not None
                  else hawkes_params["mu_buy"] + hawkes_params["mu_sell"])
        lv_ref = max(lv_ref, 1e-9)

        return {
            "ticker": ticker, "date": date,
            "td": td, "sides": sides, "t_event": t_event,
            "cold_start_params": cold_start_params, "lv_ref": lv_ref,
            "leg_class": ev.get("leg_class", "unknown"),
        }

    except Exception as e:
        log.warning("%s %s preprocessing failed: %s", ticker, date, e)
        return None


# ══════════════════════════════════════════════════════════════════════
#  Gate signal trace extraction
# ══════════════════════════════════════════════════════════════════════

def run_gate_trace(cfg: dict, ev_data: dict) -> Optional[dict]:
    """
    Replay one config on one preprocessed event and extract signal traces.

    Returns dict with: t_sec, prices, gate_states, signal, threshold,
                       entries, exits, cooling_active_intervals (if applicable),
                       norm_slope_trace (SlopeGate), lambda_v_peak_trace (level gate)
    """
    td = ev_data["td"]
    sides = ev_data["sides"]
    t_event = ev_data["t_event"]
    lv_ref = ev_data["lv_ref"]
    variant = cfg.get("variant", "a")
    N = td.n_trades

    t_sec = list(td.t_sec)
    prices = [float(td.prices[i]) for i in range(N)]

    if variant in ("a",):
        gate = ParticipationGate(
            half_life_seconds=cfg["tau"],
            peak_threshold_p=cfg["p_open"],
            warmup_seconds=EPG_WARMUP,
            p_open=cfg["p_open"],
            p_close=cfg["p_close"],
            m_cool_sec=cfg.get("m_cool_sec", 0.0),
            tau_cool_sec=cfg.get("tau_cool_sec", 120.0),
        )
        gate.activate(t_event)

        lv_trace = []
        peak_trace = []
        p_open_thr_trace = []
        p_close_thr_trace = []
        cooling_active_trace = []
        states = []

        for i in range(N):
            dv = float(td.prices[i]) * float(td.sizes[i])
            state = gate.update(dv, td.t_sec[i])
            states.append(state)
            lv_trace.append(gate._lambda_v)
            peak_trace.append(gate._lambda_v_peak)
            p_open_thr_trace.append(gate.p_open * gate._lambda_v_peak)
            p_close_thr_trace.append(gate.p_close * gate._lambda_v_peak)
            cooling_active_trace.append(gate._cooling_active)

        return {
            "t_sec": t_sec, "prices": prices, "states": states,
            "signal": lv_trace,
            "threshold_open": p_open_thr_trace,
            "threshold_close": p_close_thr_trace,
            "cooling_active": cooling_active_trace,
            "gate_type": "level",
        }

    elif variant in ("f_ss", "f_sl"):
        mode = cfg["mode"]
        gate = SlopeGate(
            tau_sec=cfg["tau"],
            L_sec=cfg["L_sec"],
            k_open=cfg["k_open"],
            mode=mode,
            k_close=cfg.get("k_close", -1.0),
            p_close=cfg.get("p_close", 0.35),
            lambda_v_ref=lv_ref,
            warmup_seconds=EPG_WARMUP,
        )
        gate.activate(t_event)

        lv_trace = []
        norm_slope_trace = []
        lv_peak_trace = []
        states = []

        for i in range(N):
            dv = float(td.prices[i]) * float(td.sizes[i])
            state = gate.update(dv, td.t_sec[i])
            states.append(state)
            lv_trace.append(gate._lambda_v)
            norm_slope_trace.append(gate.norm_slope)
            lv_peak_trace.append(gate._lambda_v_peak)

        return {
            "t_sec": t_sec, "prices": prices, "states": states,
            "signal": lv_trace,
            "norm_slope": norm_slope_trace,
            "lv_peak": lv_peak_trace,
            "gate_type": f"slope_{mode}",
        }

    return None


# ══════════════════════════════════════════════════════════════════════
#  OHLCV resampling (10s candles)
# ══════════════════════════════════════════════════════════════════════

def resample_10s(t_sec: list, prices: list) -> tuple:
    """Resample tick prices to 10-second OHLCV candles."""
    if not t_sec:
        return [], [], [], [], [], []
    t0 = math.floor(t_sec[0] / 10) * 10
    t_max = t_sec[-1]
    candle_ts = []
    opens, highs, lows, closes, vols = [], [], [], [], []
    idx = 0
    n = len(t_sec)
    t = t0
    while t <= t_max + 10:
        bucket = []
        while idx < n and t_sec[idx] < t + 10:
            bucket.append(prices[idx])
            idx += 1
        if bucket:
            candle_ts.append(t)
            opens.append(bucket[0])
            closes.append(bucket[-1])
            highs.append(max(bucket))
            lows.append(min(bucket))
            vols.append(len(bucket))
        t += 10
    return candle_ts, opens, highs, lows, closes, vols


# ══════════════════════════════════════════════════════════════════════
#  Chart generation
# ══════════════════════════════════════════════════════════════════════

def build_chart(cfg: dict, trace: dict, ticker: str, date: str) -> Optional[str]:
    """Build Plotly 2-panel chart. Returns HTML string or None on failure."""
    if not HAS_PLOTLY:
        return None

    t_sec = trace["t_sec"]
    prices = trace["prices"]
    states = trace["states"]
    gate_type = trace.get("gate_type", "level")
    variant = cfg.get("variant", "a")

    # PASS windows and entries/exits
    pass_windows = []  # list of (t_start, t_end)
    window_start = None
    in_pos = False
    entry_ts, exit_ts, exit_colors = [], [], []
    entry_t_sec_val = None
    entry_price_val = None

    for i, state in enumerate(states):
        prev = states[i - 1] if i > 0 else GateState.INACTIVE
        if state == GateState.PASS and prev != GateState.PASS:
            window_start = t_sec[i]
        elif state != GateState.PASS and prev == GateState.PASS:
            if window_start is not None:
                pass_windows.append((window_start, t_sec[i]))
            window_start = None

        if not in_pos:
            if state == GateState.PASS and prev in (GateState.INACTIVE, GateState.WARMUP, GateState.FAIL):
                entry_t_sec_val = t_sec[i]
                entry_price_val = prices[min(i + 1, len(prices) - 1)]
                entry_ts.append((t_sec[i], entry_price_val))
                in_pos = True
        else:
            if prev == GateState.PASS and state != GateState.PASS:
                ep = prices[min(i + 1, len(prices) - 1)]
                pnl = (ep - entry_price_val) / entry_price_val * 100 if entry_price_val else 0
                exit_ts.append((t_sec[i], ep))
                exit_colors.append("green" if pnl >= 0 else "red")
                in_pos = False

    if window_start is not None:
        pass_windows.append((window_start, t_sec[-1]))

    # Panel 2 rows
    if gate_type == "slope_sl":
        fig = make_subplots(
            rows=3, cols=1, shared_xaxes=True,
            row_heights=[0.5, 0.25, 0.25],
            vertical_spacing=0.04,
            subplot_titles=[f"{ticker} {date} — {cfg['config_id']}", "norm_slope", "λ_V / level"],
        )
    else:
        fig = make_subplots(
            rows=2, cols=1, shared_xaxes=True,
            row_heights=[0.6, 0.4],
            vertical_spacing=0.06,
            subplot_titles=[f"{ticker} {date} — {cfg['config_id']}", "Signal"],
        )

    # ── Panel 1: Candlesticks ──
    ct, co, ch, cl, cc, _ = resample_10s(t_sec, prices)
    fig.add_trace(go.Candlestick(
        x=ct, open=co, high=ch, low=cl, close=cc,
        increasing_line_color="green", decreasing_line_color="red",
        name="price", showlegend=False,
    ), row=1, col=1)

    # PASS window shading
    for (ws, we) in pass_windows:
        fig.add_vrect(x0=ws, x1=we, fillcolor="rgba(0,200,0,0.15)",
                      line_width=0, layer="below", row=1, col=1)

    # Entry / exit markers
    for (et, ep) in entry_ts:
        fig.add_trace(go.Scatter(
            x=[et], y=[ep], mode="markers",
            marker=dict(symbol="triangle-up", size=10, color="lime"),
            showlegend=False,
        ), row=1, col=1)
    for (xt, xp), col_str in zip(exit_ts, exit_colors):
        fig.add_trace(go.Scatter(
            x=[xt], y=[xp], mode="markers",
            marker=dict(symbol="triangle-down", size=10, color=col_str),
            showlegend=False,
        ), row=1, col=1)

    # ── Panel 2: Signal ──
    if gate_type == "level":
        signal = trace["signal"]
        thr_open = trace["threshold_open"]
        thr_close = trace["threshold_close"]
        has_cooling = variant == "a" and cfg.get("m_cool_sec", 0.0) > 0
        cooling_active = trace.get("cooling_active", [False] * len(t_sec))

        fig.add_trace(go.Scatter(x=t_sec, y=signal, mode="lines",
                                  line=dict(color="blue", width=1), name="λ_V"), row=2, col=1)
        fig.add_trace(go.Scatter(x=t_sec, y=thr_open, mode="lines",
                                  line=dict(color="darkgreen", width=1, dash="dash"),
                                  name=f"p_open×peak ({cfg['p_open']})"), row=2, col=1)
        if cfg.get("p_close") != cfg.get("p_open"):
            fig.add_trace(go.Scatter(x=t_sec, y=thr_close, mode="lines",
                                      line=dict(color="orange", width=1, dash="dot"),
                                      name=f"p_close×peak ({cfg['p_close']})"), row=2, col=1)

        if has_cooling:
            # Shade cooling-active intervals
            in_cool = False
            cool_start = None
            for i, (t, ca) in enumerate(zip(t_sec, cooling_active)):
                if ca and not in_cool:
                    cool_start = t
                    in_cool = True
                elif not ca and in_cool:
                    fig.add_vrect(x0=cool_start, x1=t,
                                  fillcolor="rgba(255,165,0,0.15)", line_width=0,
                                  layer="below", row=2, col=1)
                    in_cool = False
            if in_cool and cool_start is not None:
                fig.add_vrect(x0=cool_start, x1=t_sec[-1],
                              fillcolor="rgba(255,165,0,0.15)", line_width=0,
                              layer="below", row=2, col=1)

    elif gate_type == "slope_ss":
        ns = trace["norm_slope"]
        ko = cfg["k_open"]
        kc = cfg["k_close"]
        fig.add_trace(go.Scatter(x=t_sec, y=ns, mode="lines",
                                  line=dict(color="purple", width=1), name="norm_slope"), row=2, col=1)
        fig.add_hline(y=ko, line=dict(color="green", width=1, dash="dash"),
                      annotation_text=f"k_open={ko}", row=2, col=1)
        fig.add_hline(y=kc, line=dict(color="red", width=1, dash="dot"),
                      annotation_text=f"k_close={kc}", row=2, col=1)
        # Dead band shading
        fig.add_hrect(y0=kc, y1=ko, fillcolor="rgba(128,128,128,0.10)",
                      line_width=0, layer="below", row=2, col=1)

    elif gate_type == "slope_sl":
        ns = trace["norm_slope"]
        lv = trace["signal"]
        lv_peak = trace["lv_peak"]
        ko = cfg["k_open"]
        pc = cfg["p_close"]
        p_close_thr = [pc * p for p in lv_peak]

        # Upper sub-panel (row=2): norm_slope
        fig.add_trace(go.Scatter(x=t_sec, y=ns, mode="lines",
                                  line=dict(color="purple", width=1), name="norm_slope"), row=2, col=1)
        fig.add_hline(y=ko, line=dict(color="green", width=1, dash="dash"),
                      annotation_text=f"k_open={ko}", row=2, col=1)

        # Lower sub-panel (row=3): λ_V + p_close×peak
        fig.add_trace(go.Scatter(x=t_sec, y=lv, mode="lines",
                                  line=dict(color="blue", width=1), name="λ_V"), row=3, col=1)
        fig.add_trace(go.Scatter(x=t_sec, y=p_close_thr, mode="lines",
                                  line=dict(color="orange", width=1, dash="dash"),
                                  name=f"p_close×peak ({pc})"), row=3, col=1)

    fig.update_layout(
        height=600 if gate_type != "slope_sl" else 750,
        margin=dict(l=50, r=30, t=60, b=40),
        showlegend=True,
        xaxis_rangeslider_visible=False,
    )

    return fig.to_html(full_html=True, include_plotlyjs="cdn")


# ══════════════════════════════════════════════════════════════════════
#  Index HTML generators
# ══════════════════════════════════════════════════════════════════════

def write_config_index(config_id: str, chart_files: list[dict], config_dir: Path) -> None:
    """Write per-config index.html."""
    rows = "\n".join(
        f'<tr><td><a href="{f["filename"]}">{f["ticker"]}</a></td>'
        f'<td>{f["date"]}</td><td>{f["leg_class"]}</td>'
        f'<td>{f.get("n_windows_ge90s","?")}</td></tr>'
        for f in sorted(chart_files, key=lambda x: (x["leg_class"], x["date"]))
    )
    html = f"""<!DOCTYPE html>
<html><head><title>{config_id}</title>
<style>table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ccc;padding:6px;text-align:left}}
th{{background:#eee}}</style></head>
<body><h2>{config_id}</h2>
<table><tr><th>Ticker</th><th>Date</th><th>Leg Class</th><th>Win≥90s</th></tr>
{rows}
</table></body></html>"""
    (config_dir / "index.html").write_text(html, encoding="utf-8")


def write_master_index(all_entries: list[dict], out_dir: Path) -> None:
    """Write master_index.html."""
    rows = "\n".join(
        f'<tr><td>{e["stage"]}</td><td>{e["variant"]}</td>'
        f'<td><a href="{e["config_id"]}/index.html">{e["config_id"]}</a></td>'
        f'<td>{e.get("borda_score","?")}</td>'
        f'<td>{e["ticker"]}</td><td>{e["date"]}</td><td>{e["leg_class"]}</td>'
        f'<td><a href="{e["config_id"]}/{e["filename"]}">view</a></td></tr>'
        for e in all_entries
    )
    html = f"""<!DOCTYPE html>
<html><head><title>Phase EPG-OPT2 Charts</title>
<style>body{{font-family:sans-serif;font-size:13px}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ccc;padding:4px;text-align:left}}
th{{background:#eee;cursor:pointer}}</style></head>
<body><h1>Phase EPG-OPT2 — Master Chart Index ({len(all_entries)} charts)</h1>
<table id="t">
<tr><th>Stage</th><th>Variant</th><th>Config</th><th>Borda</th>
<th>Ticker</th><th>Date</th><th>Leg Class</th><th>Chart</th></tr>
{rows}
</table></body></html>"""
    (out_dir / "master_index.html").write_text(html, encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════

def main() -> None:
    if not HAS_PLOTLY:
        log.error("plotly not installed — cannot generate charts.")
        sys.exit(1)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    chart_events = json.load(open(CHART_EVENTS_PATH))
    log.info("Chart events: %d", len(chart_events))

    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_params = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    # Preprocess all chart events (Hawkes MLE — heavy)
    log.info("Preprocessing %d chart events (Hawkes MLE)...", len(chart_events))
    preprocessed: dict[tuple, Optional[dict]] = {}
    for ev in chart_events:
        key = (ev["ticker"], ev["date"])
        log.info("  %s_%s...", ev["ticker"], ev["date"])
        preprocessed[key] = preprocess_event(ev, hawkes_params, q_bar_cfg)

    n_prep_ok = sum(1 for v in preprocessed.values() if v is not None)
    log.info("Preprocessing done: %d/%d ok", n_prep_ok, len(chart_events))

    # Load configs
    configs = load_chart_configs()
    log.info("Configs to chart: %d", len(configs))

    # Determine stage for each config
    def get_stage(cfg: dict) -> str:
        cid = cfg["config_id"]
        v = cfg.get("variant", "a")
        if v in ("f_ss", "f_sl"):
            return "stage3"
        if cid.startswith("s2_"):
            if T120_BASE_ID in cid or cfg.get("base_config_id") == T120_BASE_ID:
                return "stage2_t120"
            return "stage2_other"
        return "stage1"

    n_charts = 0
    n_failed = 0
    all_master_entries: list[dict] = []
    total_expected = len(configs) * n_prep_ok

    t0 = time.time()
    for cfg_idx, cfg in enumerate(configs):
        config_id = cfg["config_id"]
        config_dir = OUT_DIR / config_id
        config_dir.mkdir(exist_ok=True)

        stage = get_stage(cfg)
        borda = cfg.get("borda_score")

        log.info("[%d/%d] %s", cfg_idx + 1, len(configs), config_id)

        config_chart_files = []
        for ev in chart_events:
            key = (ev["ticker"], ev["date"])
            ev_data = preprocessed.get(key)
            if ev_data is None:
                log.warning("  skip %s_%s (preprocessing failed)", ev["ticker"], ev["date"])
                n_failed += 1
                continue

            try:
                trace = run_gate_trace(cfg, ev_data)
                if trace is None:
                    n_failed += 1
                    continue

                html = build_chart(cfg, trace, ev["ticker"], ev["date"])
                if html is None:
                    n_failed += 1
                    continue

                filename = f"{ev['ticker']}_{ev['date']}.html"
                (config_dir / filename).write_text(html, encoding="utf-8")
                n_charts += 1

                chart_entry = {
                    "ticker": ev["ticker"], "date": ev["date"],
                    "leg_class": ev.get("leg_class", "unknown"),
                    "n_windows_ge90s": ev.get("n_windows_ge90s", 0),
                    "filename": filename,
                }
                config_chart_files.append(chart_entry)
                all_master_entries.append({
                    "stage": stage, "variant": cfg.get("variant", "a"),
                    "config_id": config_id, "borda_score": borda,
                    **chart_entry,
                })

            except Exception as e:
                log.warning("  chart failed %s_%s: %s", ev["ticker"], ev["date"], e)
                n_failed += 1

        write_config_index(config_id, config_chart_files, config_dir)

    write_master_index(all_master_entries, OUT_DIR)
    log.info("Master index: %s", OUT_DIR / "master_index.html")

    elapsed = time.time() - t0
    failure_rate = n_failed / max(total_expected, 1)
    log.info("\nT7 complete: %d charts in %.1fs", n_charts, elapsed)
    log.info("Failed: %d / %d (%.1f%%)", n_failed, total_expected, failure_rate * 100)

    if failure_rate > CHART_FAILURE_THRESHOLD:
        log.error("T7 ESCALATION: chart failure rate %.1f%% > 10%%. "
                  "Post error log and await instruction.", failure_rate * 100)
        sys.exit(2)


if __name__ == "__main__":
    main()
