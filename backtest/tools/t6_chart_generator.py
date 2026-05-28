"""
T6 — Generate 1,290 Plotly 2-panel HTML charts (129 configs × 10 chart events).

For each config × chart event:
  Top panel:    Price line (tick-by-tick), PASS state shaded green
  Bottom panel: Gate signal trace + threshold line

Output layout:
  results/phase_epg_grt/charts/
    {config_id}/
      {ticker}_{date}.html       (2-panel chart)
      index.html                  (10-event gallery for this config)
    master_index.html             (all 129 configs × 10 events)

Each config is run on all 10 chart events; Hawkes MLE is computed once per event
and reused across all 129 gate configs.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from data.loaders.trades import load_trades, compute_lambda_ref_per_event
from data.loaders.quotes import load_quotes
from data.schemas.mom_db import CONFIG_DIR, NS_PER_SECOND
from core.epg.anchor import EventAnchor
from core.epg.gate import ParticipationGate, GateState
from core.epg.gate_variants import (
    AbsoluteThresholdGate,
    HawkesCumulativeGate,
    HawkesBuySideGate,
    BurstRatioGate,
)
from core.ofi.trade_ofi import compute_trade_ofi
from core.hawkes.forgetting import fit_hawkes_forgetting, fit_online, HawkesParams
from tools.t3_sweep_runner import (
    build_configs,
    _hawkes_replay_with_refit,
    compute_global_fallback_ref,
    EPG_K,
    EPG_WARMUP,
)

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
except ImportError:
    print("ERROR: plotly not installed. Run: pip install plotly", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

CHART_EVENTS_PATH = REPO_ROOT / "results" / "phase_epg_grt" / "chart_events.json"
OUT_DIR = REPO_ROOT / "results" / "phase_epg_grt" / "charts"
OHLC_BIN_SEC = 60.0


# ── Gate signal extraction ────────────────────────────────────────────

def _make_gate(cfg: dict, global_fallback_ref: float, default_mu_buy: float, default_mu_sell: float, cold_start_params=None):
    """Instantiate a fresh gate from config dict."""
    variant = cfg["variant"]
    if variant == "a":
        return ParticipationGate(
            half_life_seconds=cfg["tau"],
            peak_threshold_p=cfg["p_open"],
            p_open=cfg["p_open"],
            p_close=cfg["p_close"],
        )
    elif variant == "b":
        return AbsoluteThresholdGate(
            k_abs=cfg["k_abs"],
            half_life_seconds=cfg.get("tau", 300.0),
            global_fallback_ref=global_fallback_ref,
            warmup_seconds=EPG_WARMUP,
        )
    elif variant == "c":
        mu_cum = (
            cold_start_params.mu_buy + cold_start_params.mu_sell
            if cold_start_params is not None
            else default_mu_buy + default_mu_sell
        )
        return HawkesCumulativeGate(
            beta_slow=cfg["beta_slow"],
            k_slow=cfg["k_slow"],
            mu_cum=max(mu_cum, 1e-6),
            warmup_seconds=EPG_WARMUP,
        )
    elif variant == "d":
        mu_buy = cold_start_params.mu_buy if cold_start_params is not None else default_mu_buy
        return HawkesBuySideGate(
            beta_slow=cfg["beta_slow"],
            k_slow=cfg["k_slow"],
            mu_buy=max(mu_buy, 1e-6),
            warmup_seconds=EPG_WARMUP,
        )
    elif variant == "e":
        return BurstRatioGate(
            window_n=int(cfg["window_n"]),
            threshold_r=cfg["threshold_r"],
            warmup_seconds=EPG_WARMUP,
        )
    else:
        raise ValueError(f"Unknown variant: {variant}")


def run_gate_for_chart(
    cfg: dict,
    td,
    sides: np.ndarray,
    t_event: float,
    cold_start_params,
    global_fallback_ref: float,
    default_mu_buy: float,
    default_mu_sell: float,
) -> dict:
    """
    Replay gate on one event. Returns signal traces for charting.

    Returns dict with keys:
      t_sec:          array of tick timestamps
      prices:         array of tick prices
      gate_state:     list of GateState per tick
      gate_signal:    array of gate signal value per tick (variant-specific)
      gate_threshold: array of threshold value per tick (for FAIL→PASS comparison)
      t_event:        float
      t_warmup_end:   float (t_event + EPG_WARMUP)
    """
    gate = _make_gate(cfg, global_fallback_ref, default_mu_buy, default_mu_sell, cold_start_params)
    gate.activate(t_event)

    N = td.n_trades
    gate_states = []
    gate_signals = []
    gate_thresholds = []

    for i in range(N):
        dv = float(td.prices[i]) * float(td.sizes[i])
        state = gate.update(dv, td.t_sec[i], int(sides[i]))
        gate_states.append(state)

        # Extract signal and threshold from gate internals by variant
        v = cfg["variant"]
        if v == "a":
            sig = getattr(gate, "_lambda_v", 0.0)
            thr = getattr(gate, "threshold", 0.0)
        elif v == "b":
            sig = getattr(gate, "_lambda_v", 0.0)
            ref = getattr(gate, "_lambda_v_ref", 0.0)
            thr = cfg["k_abs"] * ref if ref > 0 else 0.0
        elif v == "c":
            sig = gate._lambda_cum
            thr = cfg["k_slow"] * gate.mu_cum
        elif v == "d":
            sig = gate._lambda_buy
            thr = cfg["k_slow"] * gate.mu_buy
        elif v == "e":
            sig = gate.burst_ratio
            thr = cfg["threshold_r"]
        else:
            sig = 0.0
            thr = 0.0

        gate_signals.append(sig)
        gate_thresholds.append(thr)

    return {
        "t_sec": td.t_sec,
        "prices": np.array(td.prices, dtype=float),
        "gate_states": gate_states,
        "gate_signals": np.array(gate_signals),
        "gate_thresholds": np.array(gate_thresholds),
        "t_event": t_event,
        "t_warmup_end": t_event + EPG_WARMUP,
    }


# ── OHLC aggregation ──────────────────────────────────────────────────

def ticks_to_ohlc(t_sec: np.ndarray, prices: np.ndarray, bin_sec: float = OHLC_BIN_SEC):
    """Aggregate tick prices into OHLC bars."""
    if len(t_sec) == 0:
        return [], [], [], [], []

    t0 = t_sec[0]
    bins = np.floor((t_sec - t0) / bin_sec).astype(int)

    bar_t, bar_o, bar_h, bar_l, bar_c = [], [], [], [], []
    for b in range(bins[-1] + 1):
        mask = bins == b
        if not mask.any():
            continue
        p = prices[mask]
        ts = t_sec[mask]
        bar_t.append(float(ts[0]) + b * bin_sec)
        bar_o.append(float(p[0]))
        bar_h.append(float(p.max()))
        bar_l.append(float(p.min()))
        bar_c.append(float(p[-1]))

    return bar_t, bar_o, bar_h, bar_l, bar_c


# ── Chart builder ─────────────────────────────────────────────────────

def build_chart(
    chart_data: dict,
    cfg: dict,
    ticker: str,
    date: str,
    mom_pct: float,
) -> go.Figure:
    """Build a 2-panel Plotly figure for one config × event."""
    t = chart_data["t_sec"]
    prices = chart_data["prices"]
    states = chart_data["gate_states"]
    signals = chart_data["gate_signals"]
    thresholds = chart_data["gate_thresholds"]
    t_event = chart_data["t_event"]
    t_warmup_end = chart_data["t_warmup_end"]

    N = len(t)

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.65, 0.35],
        vertical_spacing=0.04,
        subplot_titles=[
            f"{ticker} {date} (+{mom_pct:.1f}%) — {cfg['config_id']}",
            f"Gate signal — variant {cfg['variant'].upper()}",
        ],
    )

    # ── Top panel: price line ──
    fig.add_trace(
        go.Scatter(
            x=list(t), y=list(prices),
            mode="lines",
            line=dict(color="#1f77b4", width=1),
            name="Price",
            showlegend=True,
        ),
        row=1, col=1,
    )

    # PASS shading on top panel
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
                fillcolor="rgba(0,200,0,0.12)", layer="below", line_width=0,
                row=1, col=1,
            )
    if in_pass:
        fig.add_vrect(
            x0=pass_start, x1=float(t[-1]),
            fillcolor="rgba(0,200,0,0.12)", layer="below", line_width=0,
            row=1, col=1,
        )

    # T_event vertical line
    fig.add_vline(x=t_event, line_dash="dash", line_color="orange",
                  annotation_text="T_event", annotation_position="top left", row=1, col=1)

    # Warmup end line
    if t_warmup_end <= float(t[-1]):
        fig.add_vline(x=t_warmup_end, line_dash="dot", line_color="gray",
                      annotation_text="warmup end", annotation_position="top left", row=1, col=1)

    # ── Bottom panel: gate signal + threshold ──
    fig.add_trace(
        go.Scatter(
            x=list(t), y=list(signals),
            mode="lines",
            line=dict(color="#ff7f0e", width=1),
            name="Signal",
            showlegend=True,
        ),
        row=2, col=1,
    )

    fig.add_trace(
        go.Scatter(
            x=list(t), y=list(thresholds),
            mode="lines",
            line=dict(color="#d62728", width=1, dash="dot"),
            name="Threshold",
            showlegend=True,
        ),
        row=2, col=1,
    )

    # PASS shading on bottom panel
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
                fillcolor="rgba(0,200,0,0.10)", layer="below", line_width=0,
                row=2, col=1,
            )
    if in_pass:
        fig.add_vrect(
            x0=pass_start, x1=float(t[-1]),
            fillcolor="rgba(0,200,0,0.10)", layer="below", line_width=0,
            row=2, col=1,
        )

    # ── Layout ──
    _label = _variant_signal_label(cfg)
    fig.update_yaxes(title_text="Price ($)", row=1, col=1)
    fig.update_yaxes(title_text=_label, row=2, col=1)
    fig.update_xaxes(title_text="Seconds since session start", row=2, col=1)

    fig.update_layout(
        height=640,
        template="plotly_white",
        hovermode="x unified",
        margin=dict(l=60, r=20, t=60, b=40),
        legend=dict(orientation="h", y=-0.12),
    )
    return fig


def _variant_signal_label(cfg: dict) -> str:
    v = cfg["variant"]
    if v == "a":
        return "λ_V (EMA dollar vol)"
    elif v == "b":
        return "λ_V vs k·λ_ref"
    elif v == "c":
        return "λ_cum (Hawkes all)"
    elif v == "d":
        return "λ_buy (Hawkes buy)"
    elif v == "e":
        return "Burst ratio (fast/slow)"
    return "Signal"


# ── Per-event preprocessing ───────────────────────────────────────────

def preprocess_event(
    ticker: str,
    date: str,
    mom_pct: float,
    hawkes_params: dict,
    q_bar_cfg: dict,
) -> Optional[dict]:
    """Load, classify, and run Hawkes MLE for one event. Returns None on failure."""
    try:
        td = load_trades(ticker, date, mom_pct)
        if td.n_trades < 30:
            log.warning("%s %s: insufficient trades (%d)", ticker, date, td.n_trades)
            return None

        qd = load_quotes(ticker, date, mom_pct)
        if qd is None or qd.n_quotes < 10:
            log.warning("%s %s: insufficient quotes", ticker, date)
            return None

        N = td.n_trades
        tier_qbar = q_bar_cfg.get("wide", {}).get("median", 250.0)
        ofi_result = compute_trade_ofi(
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
        sides = ofi_result.sides

        lam_buy_out = np.zeros(N, dtype=np.float64)
        lam_sell_out = np.zeros(N, dtype=np.float64)
        E_out = np.zeros(N, dtype=np.float64)
        Edot_out = np.zeros(N, dtype=np.float64)
        n_base_out = np.zeros(N, dtype=np.float64)

        global_lref = hawkes_params["mu_buy"] + hawkes_params["mu_sell"]
        per_event_lref = compute_lambda_ref_per_event(ticker, date)
        lambda_ref = (per_event_lref
                      if not math.isnan(per_event_lref) and per_event_lref > 0
                      else global_lref)

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
            log.warning("%s %s: no T_event detected", ticker, date)
            return None

        return {
            "td": td,
            "sides": sides,
            "t_event": t_event,
            "cold_start_params": cold_start_params,
            "default_mu_buy": hawkes_params["mu_buy"],
            "default_mu_sell": hawkes_params["mu_sell"],
        }

    except Exception as e:
        log.error("%s %s: preprocess failed: %s", ticker, date, e)
        return None


# ── Index HTML builders ───────────────────────────────────────────────

def build_config_index(config_id: str, events: list[dict], chart_files: list[str]) -> str:
    rows = ""
    for ev, fname in zip(events, chart_files):
        rows += (
            f"<tr><td>{ev['ticker']}</td><td>{ev['date']}</td>"
            f"<td>{ev['tercile']}</td><td>{ev['mom_pct']:.1f}%</td>"
            f"<td><a href='{fname}'>chart</a></td></tr>\n"
        )
    return f"""<!DOCTYPE html>
<html><head><title>{config_id}</title>
<style>body{{font-family:monospace;padding:20px}} table{{border-collapse:collapse}} td,th{{border:1px solid #ccc;padding:4px 8px}}</style>
</head><body>
<h2>{config_id}</h2>
<table><tr><th>ticker</th><th>date</th><th>tercile</th><th>mom_pct</th><th>chart</th></tr>
{rows}</table>
<p><a href='../master_index.html'>← master index</a></p>
</body></html>"""


def build_master_index(all_configs: list[dict], config_events: dict[str, list[str]]) -> str:
    rows = ""
    for cfg in all_configs:
        cid = cfg["config_id"]
        fnames = config_events.get(cid, [])
        links = " ".join(
            f"<a href='{cid}/{fn}'>{i+1}</a>"
            for i, fn in enumerate(fnames)
        )
        rows += (
            f"<tr><td>{cfg['variant'].upper()}</td>"
            f"<td><a href='{cid}/index.html'>{cid}</a></td>"
            f"<td>{links}</td></tr>\n"
        )
    return f"""<!DOCTYPE html>
<html><head><title>Phase EPG-GRT Charts</title>
<style>body{{font-family:monospace;padding:20px}} table{{border-collapse:collapse}} td,th{{border:1px solid #ccc;padding:4px 8px}}</style>
</head><body>
<h2>Phase EPG-GRT — Chart Gallery (129 configs × 10 events)</h2>
<table><tr><th>variant</th><th>config_id</th><th>charts (1=top1 … 10=bot3)</th></tr>
{rows}</table>
</body></html>"""


# ── Main ──────────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--configs", nargs="*", help="Subset of config_ids to generate (default: all)")
    args = parser.parse_args()

    with open(CHART_EVENTS_PATH) as f:
        chart_events_data = json.load(f)
    chart_events = chart_events_data["events"]
    log.info("Chart events: %d", len(chart_events))

    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_params = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    all_configs = build_configs()
    if args.configs:
        all_configs = [c for c in all_configs if c["config_id"] in set(args.configs)]
        log.info("Filtered to %d configs: %s", len(all_configs), args.configs)
    else:
        log.info("All %d configs", len(all_configs))

    # Compute global_fallback_ref for Variant B
    needs_b = any(c["variant"] == "b" for c in all_configs)
    if needs_b:
        global_fallback_ref = compute_global_fallback_ref(
            chart_events, hawkes_params, n_workers=4
        )
    else:
        global_fallback_ref = 0.0

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Preprocess all 10 chart events (Hawkes MLE once per event) ──
    log.info("Preprocessing %d chart events (Hawkes MLE)...", len(chart_events))
    preprocessed: dict[str, Optional[dict]] = {}
    for ev in chart_events:
        key = f"{ev['ticker']}_{ev['date']}"
        log.info("  %s...", key)
        preprocessed[key] = preprocess_event(
            ev["ticker"], ev["date"], ev["mom_pct"], hawkes_params, q_bar_cfg
        )

    t_total = time.time()
    n_charts = 0
    config_event_files: dict[str, list[str]] = {}

    # ── Generate charts ──
    for i, cfg in enumerate(all_configs):
        cid = cfg["config_id"]
        cfg_dir = OUT_DIR / cid
        cfg_dir.mkdir(exist_ok=True)

        chart_files = []
        log.info("[%d/%d] %s", i + 1, len(all_configs), cid)

        for ev in chart_events:
            key = f"{ev['ticker']}_{ev['date']}"
            pre = preprocessed.get(key)
            fname = f"{ev['ticker']}_{ev['date']}.html"

            if pre is None:
                log.warning("  skip %s (preprocessing failed)", key)
                chart_files.append(fname)
                continue

            try:
                chart_data = run_gate_for_chart(
                    cfg,
                    pre["td"],
                    pre["sides"],
                    pre["t_event"],
                    pre["cold_start_params"],
                    global_fallback_ref,
                    pre["default_mu_buy"],
                    pre["default_mu_sell"],
                )
                fig = build_chart(chart_data, cfg, ev["ticker"], ev["date"], ev["mom_pct"])
                out_path = cfg_dir / fname
                fig.write_html(str(out_path), include_plotlyjs="cdn", full_html=True)
                n_charts += 1
            except Exception as e:
                log.error("  ERROR %s × %s: %s", cid, key, e)

            chart_files.append(fname)

        # Write per-config index
        idx_html = build_config_index(cid, chart_events, chart_files)
        (cfg_dir / "index.html").write_text(idx_html, encoding="utf-8")
        config_event_files[cid] = chart_files

    # Write master index
    master_html = build_master_index(all_configs, config_event_files)
    (OUT_DIR / "master_index.html").write_text(master_html, encoding="utf-8")

    elapsed = time.time() - t_total
    log.info("\nT6 complete: %d charts in %.1fs", n_charts, elapsed)
    log.info("Master index: %s", OUT_DIR / "master_index.html")


if __name__ == "__main__":
    main()
