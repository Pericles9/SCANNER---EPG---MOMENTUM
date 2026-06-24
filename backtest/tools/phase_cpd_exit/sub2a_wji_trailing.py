"""
Phase CPD-EXIT Sub-Phase 2a — WJI Trailing Stop
================================================
WJI trailing stop on top of BOCPD winner entry gate.

Entry gate: BOCPD winner config (lambda_h=0.01, p_enter=0.60).
Sweep: p_exit ∈ {0.1,...,0.9} × variant ∈ {raw, log} = 18 configs.
Exit priority (first fires wins):
  1. WJI trailing stop:
       raw: WJI(t) < p_exit × WJI_peak_since_entry
       log: log(WJI(t)) < p_exit × log(WJI_peak_since_entry)
            [skipped per trade if log(WJI_peak) ≤ 0, i.e. peak ≤ 1.0]
  2. LULD upper — RTH upper-band proximity exit (N=1, Phase F config)
  3. EPG window close — PASS → not-PASS gate transition

WJI_peak_since_entry initialised at WJI(entry tick), updated each tick.
EXIT_D remains permanently disabled.

Outputs:
  results/phase_cpd_exit/sub2a_wji_trailing/sweep_results.json
  results/phase_cpd_exit/sub2a_wji_trailing/sweep_summary.html
  results/phase_cpd_exit/sub2a_wji_trailing/event_charts/{TICKER}_{DATE}.html
  results/phase_cpd_exit/sub2a_wji_trailing/event_charts/index.html

Run:
  "D:/Trading Research/.venv/Scripts/python.exe" -m tools.phase_cpd_exit.sub2a_wji_trailing
"""
from __future__ import annotations

import json
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
from core.ofi.trade_ofi import compute_trade_ofi
from core.epg.gate import ParticipationGate, GateState
from core.exits.luld_proximity import LuldProximityExit, ProximityState
from tools.sweep_runner_opt2 import precompute_sf_trajectory, sf_is_qualified_at
from tools.phase_cpd.cpd0_t1_traces import _build_active_axis, _compute_wji_active
from tools.phase_wji_opt.scorer import compute_metrics

# ── BOCPD winner config (fixed entry gate) ────────────────────────────────────
BOCPD_LAMBDA_H  = 0.01
BOCPD_P_ENTER   = 0.60
BOCPD_P_EXIT    = 0.50   # annotation; gate derives this internally
BOCPD_PRIOR_STD = 1.0
BOCPD_DIR_THRESH = 1.0
BOCPD_MAX_RL    = 600
SIGMA_FALLBACK  = 0.209
EPG_WARMUP      = 300.0

# ── LULD (Phase F config: upper band only, N=1) ───────────────────────────────
LULD_REF_WINDOW = 300.0
LULD_N_SPREAD   = 1.0
LULD_WARMUP     = 60.0

# ── WJI trailing stop sweep grid ─────────────────────────────────────────────
P_EXIT_GRID = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
VARIANTS    = ["raw", "log"]

# ── Paths ─────────────────────────────────────────────────────────────────────
OUT_DIR     = REPO_ROOT / "results" / "phase_cpd_exit" / "sub2a_wji_trailing"
SAMPLE_PATH = REPO_ROOT / "results" / "phase_wji_poc" / "quality_sample_val.json"
CACHE_PATH  = REPO_ROOT / "results" / "phase_wji_poc" / ".cache_val_results.json"
QBAR_PATH   = REPO_ROOT / "config" / "q_bar_tiers.json"
# Sequential — ProcessPoolExecutor deadlocks on Windows (spawn workers can't
# find project path). Cap raw ticks early so all expensive passes stay bounded.
MAX_RAW_TICKS = 100_000

# ── Baselines for comparison ──────────────────────────────────────────────────
SUB1_BASELINE  = {"config": "tp5_sl5",   "pf": 1.3990, "n_trades": 687,
                  "cvar5_pct": -9.14,    "ev": 0.505}
BOCPD_BASELINE = {"config": "lh0.01_pe0.6", "pf": 1.0779, "n_trades": 1117,
                  "cvar5_pct": -20.99,   "ev": 0.1537}


# ══════════════════════════════════════════════════════════════════════════════
#  Sweep grid
# ══════════════════════════════════════════════════════════════════════════════

def build_grid() -> list[dict]:
    return [
        {"config_id": f"{variant}_pe{int(pe * 100)}",
         "p_exit": float(pe), "variant": variant}
        for variant in VARIANTS for pe in P_EXIT_GRID
    ]


# ══════════════════════════════════════════════════════════════════════════════
#  Per-event worker  (all 18 configs, 4-pass architecture)
# ══════════════════════════════════════════════════════════════════════════════

def wji_sweep_worker(args: dict) -> dict:
    ticker, date, mom_pct = args["ticker"], args["date"], args["mom_pct"]
    t_event_raw, mu_buy   = args["t_event"], args["mu_buy"]
    q_bar_cfg, configs    = args["q_bar_cfg"], args["configs"]
    year = date[:4]
    base = {"ticker": ticker, "date": date, "year": year}

    try:
        td = load_trades(ticker, date, mom_pct)
        if td.n_trades < 30:
            return {**base, "status": "skipped", "reason": "insufficient_trades"}

        # Cap raw ticks BEFORE all expensive operations.
        raw_ts  = td.timestamps[:MAX_RAW_TICKS]
        raw_px  = td.prices[:MAX_RAW_TICKS]
        raw_sz  = td.sizes[:MAX_RAW_TICKS]
        raw_sec = td.t_sec[:MAX_RAW_TICKS]

        qd = load_quotes(ticker, date, mom_pct)
        if qd is None or qd.n_quotes < 10:
            return {**base, "status": "skipped", "reason": "insufficient_quotes"}

        tier_qbar = q_bar_cfg.get("wide", {}).get("median", 250.0)
        ofi = compute_trade_ofi(
            trade_timestamps=raw_ts, trade_prices=raw_px,
            trade_sizes=raw_sz.astype(np.float64),
            quote_timestamps=qd.timestamps,
            quote_bid_prices=qd.bid_prices, quote_ask_prices=qd.ask_prices,
            quote_bid_sizes=qd.bid_sizes.astype(np.float64),
            quote_ask_sizes=qd.ask_sizes.astype(np.float64),
            window_sec=10.0, q_bar_fallback=tier_qbar,
        )
        sides_full = ofi.sides

        from data.loaders.trades import TradeData as _TD
        td_cap = _TD(timestamps=raw_ts, prices=raw_px, sizes=raw_sz, t_sec=raw_sec,
                     n_trades=len(raw_ts), ticker=ticker, date=date, mom_pct=mom_pct)

        mask, active_seconds, _, _ = _build_active_axis(td_cap)
        if mask.sum() < 30:
            return {**base, "status": "skipped", "reason": "insufficient_active_trades"}

        prices_a = raw_px[mask]
        sizes_a  = raw_sz[mask]
        sides_a  = sides_full[mask]
        ts_ns_a  = raw_ts[mask]
        t_sec_a  = raw_sec[mask]

        pos = int(np.searchsorted(t_sec_a, t_event_raw, side="right")) - 1
        t_event_active = float(active_seconds[max(pos, 0)])

        wji, _ = _compute_wji_active(prices_a, sizes_a, sides_a, active_seconds,
                                     t_event_active, mu_buy)

        start_ns, end_ns = _session_ns_bounds(date)
        sf = precompute_sf_trajectory(td_cap, start_ns, end_ns)

        N = len(wji)
        prices  = np.asarray(prices_a, dtype=np.float64)
        use_sf  = sf is not None and sf.n_bars > 0

        # ── Pass 1: BOCPD gate once → gate_seq ───────────────────────────────
        gate = ParticipationGate(
            half_life_seconds=300.0, peak_threshold_p=0.65, warmup_seconds=EPG_WARMUP,
            gate_mode="bocpd", lambda_h=BOCPD_LAMBDA_H, p_enter=BOCPD_P_ENTER,
            sigma_log_fallback=SIGMA_FALLBACK, prior_mean_std=BOCPD_PRIOR_STD,
            dir_thresh_mult=BOCPD_DIR_THRESH, max_run_length=BOCPD_MAX_RL,
        )
        gate.activate(t_event_active)
        gate_seq: list[GateState] = []
        for i in range(N):
            gate_seq.append(gate.update(wji=float(wji[i]),
                                        timestamp=float(active_seconds[i]),
                                        wji_background=1.0))

        # ── Pass 2: LULD once → luld_fire mask ───────────────────────────────
        luld  = LuldProximityExit(ref_window_sec=LULD_REF_WINDOW,
                                  n_spread_multiple=LULD_N_SPREAD,
                                  warmup_sec=LULD_WARMUP)
        nq    = qd.n_quotes
        q_idx = 0
        luld_fire = np.zeros(N, dtype=bool)
        for i in range(N):
            ts_i = int(ts_ns_a[i])
            while q_idx < nq - 1 and qd.timestamps[q_idx + 1] <= ts_i:
                q_idx += 1
            if q_idx < nq and qd.timestamps[q_idx] <= ts_i:
                bid = float(qd.bid_prices[q_idx]); ask = float(qd.ask_prices[q_idx])
                if bid <= 0.0 or ask <= bid:
                    bid = ask = None
            else:
                bid = ask = None
            lr = luld.update(ts_i, float(prices[i]), bid, ask)
            if lr.state == ProximityState.EXIT_HALT and lr.fire_side == "upper":
                luld_fire[i] = True

        # ── Pass 3: collect rising-edge entry ticks ───────────────────────────
        entries: list[tuple[int, int]] = []   # (signal_idx, entry_idx)
        prev_gs = GateState.INACTIVE
        for i, gs in enumerate(gate_seq):
            if gs == GateState.PASS and prev_gs != GateState.PASS:
                if (not use_sf) or sf_is_qualified_at(sf, int(ts_ns_a[i])):
                    entries.append((i, min(i + 1, N - 1)))
            prev_gs = gs

        max_from = np.maximum.accumulate(prices[::-1])[::-1]

        # ── Pass 4: replay each WJI trailing stop config ──────────────────────
        config_results: dict[str, list[dict]] = {}
        for cfg in configs:
            p_exit  = cfg["p_exit"]
            variant = cfg["variant"]
            trades: list[dict] = []

            for sig_i, entry_i in entries:
                entry_price  = float(prices[entry_i])
                entry_t      = float(active_seconds[sig_i])
                entry_ts     = int(ts_ns_a[sig_i])

                # Per-trade WJI peak initialised at entry tick
                wji_peak     = float(wji[entry_i])
                log_wji_peak = math.log(max(wji_peak, 1e-9))
                # Log variant only valid when log(peak) > 0 (i.e. peak > 1.0)
                log_active   = (variant == "log") and (log_wji_peak > 0.0)

                exit_type = exit_price = exit_i = None

                for j in range(entry_i, N):
                    wji_j = float(wji[j])

                    # Update running peak
                    if wji_j > wji_peak:
                        wji_peak = wji_j
                        if variant == "log":
                            log_wji_peak = math.log(max(wji_peak, 1e-9))
                            log_active   = log_wji_peak > 0.0

                    # Priority 1: WJI trailing stop
                    stop_fires = False
                    if variant == "raw":
                        stop_fires = wji_j < p_exit * wji_peak
                    elif log_active:
                        log_wji_j  = math.log(max(wji_j, 1e-9))
                        stop_fires = log_wji_j < p_exit * log_wji_peak

                    if stop_fires:
                        exit_type  = "wji_trailing_stop"
                        exit_price = float(prices[j]); exit_i = j; break

                    # Priority 2: LULD upper
                    if luld_fire[j]:
                        exit_type  = "luld_upper"
                        exit_price = float(prices[j]); exit_i = j; break

                    # Priority 3: EPG window close
                    if (j > entry_i
                            and gate_seq[j] != GateState.PASS
                            and gate_seq[j - 1] == GateState.PASS):
                        exit_price = float(prices[min(j + 1, N - 1)])
                        exit_type  = "epg_window_close"; exit_i = j; break

                if exit_type is None:
                    exit_i     = N - 1
                    exit_price = float(prices[N - 1])
                    exit_type  = "epg_window_close"

                pnl = (exit_price - entry_price) / entry_price * 100.0
                trades.append({
                    "pnl_pct":             pnl,
                    "available_move_pct":  max(max_from[entry_i] / entry_price - 1.0, 0.0) * 100.0,
                    "hold_sec":            float(active_seconds[exit_i]) - entry_t,
                    "year": year, "ticker": ticker, "date": date,
                    "exit_type":   exit_type,
                    "entry_ts_ns": entry_ts,
                    "exit_ts_ns":  int(ts_ns_a[exit_i]),
                    "entry_price": entry_price,
                    "exit_price":  exit_price,
                })

            config_results[cfg["config_id"]] = trades

        return {**base, "status": "ok", "config_results": config_results}

    except Exception as e:
        import traceback
        return {**base, "status": "error", "error": str(e),
                "traceback": traceback.format_exc()}


# ══════════════════════════════════════════════════════════════════════════════
#  Metrics helpers
# ══════════════════════════════════════════════════════════════════════════════

def _exit_breakdown(trades: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for t in trades:
        k = t.get("exit_type", "unknown")
        counts[k] = counts.get(k, 0) + 1
    n = len(trades)
    return {
        "counts": counts,
        "pcts":   {k: round(v / n * 100, 1) for k, v in counts.items()} if n else {},
    }


def _win_rate(trades: list[dict]) -> Optional[float]:
    if not trades:
        return None
    return sum(1 for t in trades if t["pnl_pct"] > 0) / len(trades)


def _mean_hold_sec(trades: list[dict]) -> Optional[float]:
    if not trades:
        return None
    return sum(t["hold_sec"] for t in trades) / len(trades)


def _pf(pnl_list: list[float]) -> float:
    wins   = sum(p for p in pnl_list if p > 0)
    losses = sum(-p for p in pnl_list if p < 0)
    if losses == 0:
        return float("inf") if wins > 0 else float("nan")
    return wins / losses


def _cvar5(pnl_list: list[float]) -> float:
    n = len(pnl_list)
    if n == 0:
        return float("nan")
    k = max(1, math.floor(0.05 * n))
    return sum(sorted(pnl_list)[:k]) / k


def build_extended_metrics(trades: list[dict]) -> dict:
    m = compute_metrics(trades)
    return {
        **m,
        "win_rate":      _win_rate(trades),
        "mean_hold_sec": _mean_hold_sec(trades),
        "exit_breakdown": _exit_breakdown(trades),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Sweep summary HTML
# ══════════════════════════════════════════════════════════════════════════════

def _write_summary_html(rows: list[dict], path: Path, best_id: str) -> None:
    cols = [
        ("config_id", "config"), ("variant", "variant"), ("p_exit", "p_exit"),
        ("pf", "PF"), ("n_trades", "n"), ("win_rate", "win%"),
        ("mean_hold_sec", "hold_s"), ("cvar5_pct", "CVaR5"),
        ("ev", "EV/trade"), ("wji_stop_pct", "wji_stop%"),
        ("luld_pct", "luld%"), ("epg_pct", "epg%"),
    ]
    head = "".join(f'<th onclick="sortBy({i})">{lbl}</th>'
                   for i, (_, lbl) in enumerate(cols))
    trs = []
    for r in rows:
        bd  = r.get("exit_breakdown", {}).get("pcts", {})
        pf  = f"{r['pf']:.3f}" if r.get("pf") is not None and math.isfinite(r["pf"]) else "inf"
        vals = {
            "config_id":     r["config_id"],
            "variant":       r["variant"],
            "p_exit":        f"{r['p_exit']:.1f}",
            "pf":            pf,
            "n_trades":      r.get("n_trades", 0),
            "win_rate":      f"{r['win_rate']*100:.1f}" if r.get("win_rate") is not None else "—",
            "mean_hold_sec": f"{r['mean_hold_sec']:.0f}" if r.get("mean_hold_sec") is not None else "—",
            "cvar5_pct":     f"{r['cvar5_pct']:.2f}" if r.get("cvar5_pct") is not None else "—",
            "ev":            f"{r['ev']:.3f}" if r.get("ev") is not None else "—",
            "wji_stop_pct":  f"{bd.get('wji_trailing_stop', 0):.1f}",
            "luld_pct":      f"{bd.get('luld_upper', 0):.1f}",
            "epg_pct":       f"{bd.get('epg_window_close', 0):.1f}",
        }
        row_class = " class='best'" if r["config_id"] == best_id else ""
        cells = "".join(f"<td>{vals[k]}</td>" for k, _ in cols)
        trs.append(f"<tr{row_class}>{cells}</tr>")

    html = f"""<!doctype html><html><head><meta charset='utf-8'>
<title>Phase CPD-EXIT Sub-Phase 2a — WJI Trailing Stop Sweep</title>
<style>
body{{font-family:sans-serif;background:#1a1a2e;color:#e0e0e0;margin:24px}}
h2{{color:#ce93d8}}p{{color:#b0bec5;font-size:13px}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #2d2d4e;padding:7px 12px;text-align:right}}
th{{cursor:pointer;background:#16213e;color:#80cbc4}}th:hover{{background:#0f3460}}
td:first-child,td:nth-child(2),th:first-child,th:nth-child(2){{text-align:left}}
tr:nth-child(even){{background:#16213e}}tr:nth-child(odd){{background:#1a1a2e}}
tr:hover{{background:#0f3460}}tr.best{{background:#0f3460!important;font-weight:bold}}
</style></head>
<body>
<h2>Phase CPD-EXIT Sub-Phase 2a — WJI Trailing Stop (18 configs)</h2>
<p>Sub-Phase 1 baseline (tp5_sl5): PF={SUB1_BASELINE['pf']:.4f} n={SUB1_BASELINE['n_trades']} CVaR5={SUB1_BASELINE['cvar5_pct']:.2f}% EV={SUB1_BASELINE['ev']:.4f} &middot;
BOCPD baseline: PF={BOCPD_BASELINE['pf']:.4f} CVaR5={BOCPD_BASELINE['cvar5_pct']:.2f}% &middot;
18 configs &middot; click header to sort</p>
<table id='t'><thead><tr>{head}</tr></thead><tbody>{''.join(trs)}</tbody></table>
<script>
var _asc={{}};
function sortBy(c){{
  var tb=document.querySelector('#t tbody'),rs=[...tb.rows];
  var a=_asc[c]===undefined?false:!_asc[c];_asc[c]=a;
  rs.sort(function(x,y){{
    var av=x.cells[c].innerText.trim(),bv=y.cells[c].innerText.trim();
    var na=parseFloat(av),nb=parseFloat(bv);
    if(!isNaN(na)&&!isNaN(nb))return a?na-nb:nb-na;
    return a?av.localeCompare(bv):bv.localeCompare(av);
  }});rs.forEach(function(r){{tb.appendChild(r);}});
}}
sortBy(3);
</script></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════════
#  Per-event charts
# ══════════════════════════════════════════════════════════════════════════════

_CHART_BG    = "#1a1a2e"
_PAPER_BG    = "#16213e"
_GRID_COLOR  = "#2d2d4e"
_TEXT_COLOR  = "#e0e0e0"
_PASS_FILL   = "rgba(0,200,80,0.12)"
_WARMUP_FILL = "rgba(255,167,38,0.12)"
_PLOT_MAX    = 5000
_WARMUP_SEC  = 300.0


def _downsample(x, *ys, max_pts=_PLOT_MAX):
    n = len(x)
    if n <= max_pts:
        return (x, *ys)
    step = int(math.ceil(n / max_pts))
    idx  = list(range(0, n, step))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    idx = np.array(idx)
    return (x[idx], *[y[idx] for y in ys])


def _pass_intervals_str(tv, gate_states):
    ivs = []; in_p = False; start = None
    for i, gs in enumerate(gate_states):
        if gs == "PASS" and not in_p:
            in_p = True; start = float(tv[i])
        elif gs != "PASS" and in_p:
            in_p = False; ivs.append((start, float(tv[i])))
    if in_p:
        ivs.append((start, float(tv[-1])))
    return ivs


def _build_wji_trailing_chart(
    result: dict,
    p_exit: float,
    variant: str,
    out_path: Path,
) -> None:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    import plotly.io as pio

    ticker      = result["ticker"]; date = result["date"]
    ticks       = result["ticks"]; trades = result["trades"]
    ohlcv       = result.get("ohlcv_10s", [])
    t_event_ns  = result.get("t_event_ns")
    n_trades    = result["n_trades"]
    event_pf    = result.get("event_pf")
    pf_str      = (f"{event_pf:.3f}" if event_pf is not None and math.isfinite(event_pf)
                   else "inf")

    if not ticks:
        return

    all_ts   = np.array([t["ts_ns"]    for t in ticks], dtype=np.int64)
    all_ta   = np.array([t["t_active"] for t in ticks], dtype=np.float64)
    all_wraw = np.array([t["wji_raw"]  for t in ticks], dtype=np.float64)
    all_gs   = [t["gate_state"] for t in ticks]
    all_pr   = np.array([
        t["p_regime"] if t["p_regime"] is not None else float("nan")
        for t in ticks], dtype=np.float64)

    if t_event_ns is not None:
        ev_pos = min(int(np.searchsorted(all_ts, t_event_ns, side="left")), len(all_ta) - 1)
        t_event_active = float(all_ta[ev_pos])
    else:
        t_event_active = float(all_ta[0])

    tse  = all_ta - t_event_active
    post = tse >= 0.0
    tv   = tse[post]
    wraw = all_wraw[post]
    pr   = all_pr[post]
    gs   = [all_gs[i] for i in range(len(all_gs)) if post[i]]
    pass_iv = _pass_intervals_str(tv, gs)

    # OHLCV candles
    candle_tse, co, ch, clo, cc = [], [], [], [], []
    if ohlcv and t_event_ns is not None:
        for bar in ohlcv:
            btse = (bar["open_ts_ns"] - t_event_ns) / 1e9
            if btse < 0:
                continue
            candle_tse.append(btse); co.append(bar["open"])
            ch.append(bar["high"]); clo.append(bar["low"]); cc.append(bar["close"])

    fig = make_subplots(
        rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.028,
        row_heights=[4, 2, 2, 1.5],
        subplot_titles=(
            "Price (10s candles)",
            "WJI raw  (bg ref = 1.0)",
            f"WJI trailing floor  [p_exit={p_exit:.1f}  variant={variant}]",
            f"P_regime  (p_enter={BOCPD_P_ENTER:.2f}  p_exit={BOCPD_P_EXIT:.2f})",
        ),
    )

    # Panel 1: candles + trade markers
    if candle_tse:
        fig.add_trace(go.Candlestick(
            x=candle_tse, open=co, high=ch, low=clo, close=cc,
            increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
            increasing_fillcolor="#26a69a", decreasing_fillcolor="#ef5350",
            showlegend=False,
        ), row=1, col=1)

    if t_event_ns is not None:
        for tr in trades:
            ets = (tr["entry_ts_ns"] - t_event_ns) / 1e9
            xts = (tr["exit_ts_ns"]  - t_event_ns) / 1e9
            win = tr["pnl_pct"] > 0
            ec  = "#00e676" if win else "#ff1744"
            fig.add_trace(go.Scatter(x=[ets], y=[tr["entry_price"]], mode="markers",
                marker=dict(symbol="triangle-up",   color=ec, size=10),
                showlegend=False), row=1, col=1)
            fig.add_trace(go.Scatter(x=[xts], y=[tr["exit_price"]], mode="markers",
                marker=dict(symbol="triangle-down", color=ec, size=10),
                showlegend=False), row=1, col=1)

    # Panel 2: WJI raw (full event, post t_event)
    dt, dw = _downsample(tv, wraw)
    fig.add_trace(go.Scatter(x=dt, y=dw, mode="lines",
        line=dict(color="#64b5f6", width=1), showlegend=False), row=2, col=1)
    fig.add_hline(y=1.0, line=dict(color="#546e7a", width=1, dash="dash"),
        annotation_text="bg=1.0", annotation_font_color=_TEXT_COLOR, row=2, col=1)

    # Panel 3: WJI raw + per-trade trailing floor lines
    fig.add_trace(go.Scatter(x=dt, y=dw, mode="lines",
        line=dict(color="#64b5f6", width=0.8, dash="dot"), showlegend=False), row=3, col=1)
    fig.add_hline(y=1.0, line=dict(color="#546e7a", width=1, dash="dash"),
        annotation_text="bg=1.0", annotation_font_color=_TEXT_COLOR, row=3, col=1)

    if t_event_ns is not None and trades:
        for tr in trades:
            ei = int(np.searchsorted(all_ts, tr["entry_ts_ns"], side="left"))
            xi = int(np.searchsorted(all_ts, tr["exit_ts_ns"],  side="right")) - 1
            xi = min(xi, len(all_wraw) - 1)
            ei = max(ei, 0)
            if ei >= xi:
                continue
            wji_slice = all_wraw[ei : xi + 1]
            ta_slice  = all_ta[ei : xi + 1]
            peak_so_far = np.maximum.accumulate(wji_slice)

            if variant == "raw":
                floor_line = p_exit * peak_so_far
            else:
                log_peak    = np.log(np.maximum(peak_so_far, 1e-9))
                valid_mask  = log_peak > 0.0
                floor_line  = np.where(valid_mask, np.exp(p_exit * log_peak), 0.0)

            tse_slice = ta_slice - t_event_active
            # Trim pre-event portion
            post_mask = tse_slice >= 0.0
            if post_mask.sum() == 0:
                continue
            tse_slice  = tse_slice[post_mask]
            floor_line = floor_line[post_mask]

            win   = tr["pnl_pct"] > 0
            fc    = "#00e676" if win else "#ff5252"
            fig.add_trace(go.Scatter(x=tse_slice, y=floor_line, mode="lines",
                line=dict(color=fc, width=1.5, dash="dash"), showlegend=False), row=3, col=1)

    # Panel 4: P_regime
    dt4, dp = _downsample(tv, pr)
    valid = ~np.isnan(dp)
    if valid.any():
        fig.add_trace(go.Scatter(x=dt4[valid], y=dp[valid], mode="lines",
            line=dict(color="#ce93d8", width=1.2), showlegend=False), row=4, col=1)
    fig.add_hline(y=BOCPD_P_ENTER, line=dict(color="#ef5350", width=1.2),
        annotation_text=f"p_enter={BOCPD_P_ENTER:.2f}",
        annotation_font_color="#ef5350", row=4, col=1)
    fig.add_hline(y=BOCPD_P_EXIT, line=dict(color="#ff8a65", width=1, dash="dash"),
        annotation_text=f"p_exit={BOCPD_P_EXIT:.2f}",
        annotation_font_color="#ff8a65", row=4, col=1)
    fig.update_yaxes(range=[0, 1], row=4, col=1)

    # Shading
    for row in range(1, 5):
        fig.add_vrect(x0=0.0, x1=_WARMUP_SEC, fillcolor=_WARMUP_FILL,
                      line_width=0, row=row, col=1)
        for a, b in pass_iv:
            fig.add_vrect(x0=a, x1=b, fillcolor=_PASS_FILL, line_width=0, row=row, col=1)
    fig.add_vline(x=0.0, line=dict(color="#b0bec5", width=1, dash="dash"), row=1, col=1)

    ax_style = dict(gridcolor=_GRID_COLOR, zeroline=False,
                    tickfont=dict(color=_TEXT_COLOR), title_font=dict(color=_TEXT_COLOR))
    for i in range(1, 5):
        fig.update_xaxes(**ax_style, row=i, col=1, rangeslider_visible=False)
        fig.update_yaxes(**ax_style, row=i, col=1)
    fig.update_xaxes(title_text="active seconds since T_event", row=4, col=1)

    fig.update_layout(
        height=950, paper_bgcolor=_PAPER_BG, plot_bgcolor=_CHART_BG,
        font=dict(color=_TEXT_COLOR, size=11), showlegend=False,
        margin=dict(l=60, r=30, t=60, b=40),
        title=dict(
            text=(f"{ticker} {date} | WJI trailing  variant={variant}  p_exit={p_exit:.1f}"
                  f" | PF={pf_str}  n={n_trades}"),
            font=dict(color=_TEXT_COLOR, size=14),
        ),
    )
    for ann in fig.layout.annotations:
        ann.font.color = _TEXT_COLOR

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pio.write_html(fig, str(out_path), include_plotlyjs=True, auto_open=False)


def _write_chart_index(rows: list[dict], path: Path, best_cfg: dict) -> None:
    variant = best_cfg["variant"]; p_exit = best_cfg["p_exit"]
    cols = [("ticker", "Ticker"), ("date", "Date"), ("n_trades", "n"),
            ("event_pf", "event_PF"), ("cvar5_event", "CVaR5_event"),
            ("worst_trade", "worst%"), ("dom_exit", "dom_exit"), ("link", "chart")]
    head = "".join(f'<th onclick="sortBy({i})">{lbl}</th>'
                   for i, (_, lbl) in enumerate(cols))
    trs = []
    for r in rows:
        pf  = (f"{r['event_pf']:.3f}"
               if r.get("event_pf") is not None and math.isfinite(r.get("event_pf", float("nan")))
               else "inf")
        cv  = f"{r['cvar5_event']:.2f}" if r.get("cvar5_event") is not None else "N/A"
        wt  = f"{r['worst_trade']:.2f}"  if r.get("worst_trade") is not None else "N/A"
        fn  = f"{r['ticker']}_{r['date']}.html"
        cells = [f"<td>{r['ticker']}</td>", f"<td>{r['date']}</td>",
                 f"<td>{r['n_trades']}</td>", f"<td>{pf}</td>", f"<td>{cv}</td>",
                 f"<td>{wt}</td>", f"<td>{r.get('dom_exit','—')}</td>",
                 f"<td><a href='./{fn}'>chart</a></td>"]
        trs.append("<tr>" + "".join(cells) + "</tr>")
    html = f"""<!doctype html><html><head><meta charset='utf-8'>
<title>CPD-EXIT Sub2a WJI trailing event charts</title>
<style>body{{font-family:sans-serif;background:#1a1a2e;color:#e0e0e0;margin:24px}}
h2{{color:#ce93d8}}p{{color:#b0bec5;font-size:13px}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #2d2d4e;padding:7px 12px;text-align:right}}
th{{cursor:pointer;background:#16213e;color:#80cbc4}}th:hover{{background:#0f3460}}
td:first-child,th:first-child{{text-align:left}}td:last-child{{text-align:center}}
tr:nth-child(even){{background:#16213e}}tr:nth-child(odd){{background:#1a1a2e}}
tr:hover{{background:#0f3460}}a{{color:#64b5f6;text-decoration:none}}</style></head>
<body>
<h2>CPD-EXIT Sub-Phase 2a — Best WJI config ({variant} p_exit={p_exit:.1f}) event charts</h2>
<p>{len(rows)} events with trades &middot; click header to sort &middot; default: event_PF desc</p>
<table id='t'><thead><tr>{head}</tr></thead><tbody>{''.join(trs)}</tbody></table>
<script>
var _asc={{}};
function sortBy(c){{
  var tb=document.querySelector('#t tbody'),rs=[...tb.rows];
  var a=_asc[c]===undefined?false:!_asc[c];_asc[c]=a;
  rs.sort(function(x,y){{
    var av=x.cells[c].innerText.trim(),bv=y.cells[c].innerText.trim();
    var na=parseFloat(av),nb=parseFloat(bv);
    if(!isNaN(na)&&!isNaN(nb))return a?na-nb:nb-na;
    return a?av.localeCompare(bv):bv.localeCompare(av);
  }});rs.forEach(function(r){{tb.appendChild(r);}});
}}
sortBy(3);
</script></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════════
#  Shared helpers
# ══════════════════════════════════════════════════════════════════════════════

BOCPD_REPLAY_CACHE = (REPO_ROOT / "results" / "phase_cpd_bocpd" / "viz"
                      / "replay_cache.json")


def _load_replay_cache() -> dict:
    if not BOCPD_REPLAY_CACHE.exists():
        return {}
    with BOCPD_REPLAY_CACHE.open(encoding="utf-8") as f:
        d = json.load(f)
    return d.get("events", {})


def _write_json(data, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    def _default(o):
        if isinstance(o, float) and math.isnan(o):
            return None
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.integer):
            return int(o)
        return str(o)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, suffix=".tmp",
                                     delete=False, encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=_default)
        tmp = Path(f.name)
    os.replace(str(tmp), str(path))


# ══════════════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    events    = json.load(open(SAMPLE_PATH))["events"]
    cache     = json.load(open(CACHE_PATH))
    lut       = {(r["ticker"], r["date"]): r for r in cache
                 if r.get("status") == "ok"
                 and r.get("t_event") is not None
                 and r.get("mu_buy")  is not None}
    q_bar_cfg = json.load(open(QBAR_PATH))
    configs   = build_grid()

    work = []
    for e in events:
        c = lut.get((e["ticker"], e["date"]))
        if c is None:
            continue
        work.append({"ticker": e["ticker"], "date": e["date"],
                     "mom_pct": e["mom_pct"], "t_event": c["t_event"],
                     "mu_buy": c["mu_buy"], "q_bar_cfg": q_bar_cfg,
                     "configs": configs})

    print(f"Sub2a WJI trailing: {len(work)} events × {len(configs)} configs", flush=True)
    cfg_ids     = [c["config_id"] for c in configs]
    grid_lookup = {c["config_id"]: c for c in configs}
    trades_by_cfg: dict[str, list[dict]] = {cid: [] for cid in cfg_ids}
    n_ok = 0; errors = []
    t0 = time.time()

    for done, a in enumerate(work, 1):
        r = wji_sweep_worker(a)
        if r["status"] == "ok":
            n_ok += 1
            for cid, trs in r["config_results"].items():
                trades_by_cfg[cid].extend(trs)
        elif r["status"] == "error":
            errors.append({"ticker": r["ticker"], "date": r["date"],
                           "error": r.get("error")})
            print(f"  ERROR {r['ticker']} {r['date']}: {r.get('error')}", flush=True)
        if done % 10 == 0:
            print(f"  {done}/{len(work)} ({time.time()-t0:.0f}s)", flush=True)

    print(f"  done: {n_ok} ok, {len(errors)} errored ({time.time()-t0:.0f}s)", flush=True)

    # Build per-config metric rows
    rows = []
    for cid in cfg_ids:
        trades = trades_by_cfg[cid]
        m = build_extended_metrics(trades)
        rows.append({
            "config_id":     cid,
            **grid_lookup[cid],
            "pf":            m["pf"],
            "n_trades":      m["n_trades"],
            "win_rate":      m["win_rate"],
            "mean_hold_sec": m["mean_hold_sec"],
            "cvar5_pct":     m["cvar5_pct"],
            "ev":            m["ev"],
            "max_loss_pct":  m["max_loss_pct"],
            "exit_breakdown": m["exit_breakdown"],
        })

    def _sort_key(r):
        pf = r["pf"]
        if pf is None or (isinstance(pf, float) and not math.isfinite(pf)):
            return (-float("inf"),)
        return (-pf,)
    rows.sort(key=_sort_key)

    # Print sweep table
    print(f"\n{'config':<18}{'var':<6}{'pe':>4}{'PF':>8}{'n':>7}{'win%':>7}"
          f"{'CVaR5':>9}{'EV':>8}{'hold_s':>8}{'wji%':>7}{'luld%':>7}{'epg%':>7}")
    for r in rows:
        pf  = f"{r['pf']:.3f}" if r['pf'] is not None and math.isfinite(r['pf']) else "inf"
        cv  = f"{r['cvar5_pct']:.2f}"   if r['cvar5_pct']     is not None else "—"
        ev  = f"{r['ev']:.3f}"          if r['ev']            is not None else "—"
        wr  = f"{r['win_rate']*100:.1f}" if r['win_rate']      is not None else "—"
        hs  = f"{r['mean_hold_sec']:.0f}" if r['mean_hold_sec'] is not None else "—"
        bd  = r["exit_breakdown"].get("pcts", {})
        print(f"{r['config_id']:<18}{r['variant']:<6}{r['p_exit']:>4.1f}"
              f"{pf:>8}{r['n_trades']:>7}{wr:>7}{cv:>9}{ev:>8}{hs:>8}"
              f"{bd.get('wji_trailing_stop',0):>7.1f}"
              f"{bd.get('luld_upper',0):>7.1f}"
              f"{bd.get('epg_window_close',0):>7.1f}")

    # Escalation check
    finite_pfs = [r["pf"] for r in rows
                  if r["pf"] is not None and math.isfinite(r["pf"])]
    all_pf_below_1 = bool(finite_pfs) and all(p < 1.0 for p in finite_pfs)
    best_row = rows[0]  # already sorted by PF desc
    best_id  = best_row["config_id"]

    escalations = []
    if all_pf_below_1:
        escalations.append("ALL 18 PF < 1.0 — HARD STOP (entry quality problem)")
    if best_row.get("cvar5_pct") is not None and best_row["cvar5_pct"] < -15.0:
        escalations.append(f"BEST CVaR5 {best_row['cvar5_pct']:.2f}% < -15% — HARD STOP")

    print(f"\nBEST config: {best_id}  PF={best_row.get('pf'):.4f}"
          f"  n={best_row['n_trades']}  CVaR5={best_row.get('cvar5_pct'):.2f}%"
          f"  EV={best_row.get('ev'):.4f}")
    print(f"Sub-1 baseline (tp5_sl5): PF={SUB1_BASELINE['pf']:.4f}"
          f"  CVaR5={SUB1_BASELINE['cvar5_pct']:.2f}%  EV={SUB1_BASELINE['ev']:.4f}")
    print(f"BOCPD baseline:           PF={BOCPD_BASELINE['pf']:.4f}"
          f"  CVaR5={BOCPD_BASELINE['cvar5_pct']:.2f}%  EV={BOCPD_BASELINE['ev']:.4f}")

    _write_json({
        "sub1_baseline":   SUB1_BASELINE,
        "bocpd_baseline":  BOCPD_BASELINE,
        "best_config_id":  best_id,
        "escalations":     escalations,
        "n_events_ok":     n_ok,
        "n_errored":       len(errors),
        "errors":          errors,
        "results":         rows,
    }, OUT_DIR / "sweep_results.json")

    _write_summary_html(rows, OUT_DIR / "sweep_summary.html", best_id)

    if escalations:
        for e in escalations:
            print(f"  *** {e} ***")
        print("HARD STOP — not generating per-event charts. Awaiting instruction.")
        return

    # T4: per-event charts for best config
    print(f"\nT4: generating charts for best config {best_id}...", flush=True)
    replay_cache = _load_replay_cache()
    best_cfg     = grid_lookup[best_id]

    trades_by_event: dict[str, list[dict]] = {}
    for t in trades_by_cfg[best_id]:
        k = f"{t['ticker']}_{t['date']}"
        trades_by_event.setdefault(k, []).append(t)

    chart_dir  = OUT_DIR / "event_charts"
    index_rows = []
    chart_ok   = 0; chart_err = 0

    for key, event_trades in sorted(trades_by_event.items()):
        cache_entry = replay_cache.get(key)
        if cache_entry is None:
            continue
        pnls     = [t["pnl_pct"] for t in event_trades]
        bd       = _exit_breakdown(event_trades)
        dom      = (max(bd["counts"].items(), key=lambda x: x[1])[0]
                    if bd["counts"] else "—")
        event_pf = _pf(pnls) if pnls else None
        cvar5    = _cvar5(pnls) if len(pnls) >= 5 else None

        result = {
            **cache_entry,
            "trades": [
                {"entry_ts_ns": t["entry_ts_ns"], "exit_ts_ns":  t["exit_ts_ns"],
                 "entry_price": t["entry_price"], "exit_price":  t["exit_price"],
                 "pnl_pct":     t["pnl_pct"]}
                for t in event_trades
            ],
            "n_trades": len(event_trades),
            "event_pf": event_pf,
        }
        out_path = chart_dir / f"{key}.html"
        try:
            _build_wji_trailing_chart(result, best_cfg["p_exit"], best_cfg["variant"], out_path)
            index_rows.append({
                "ticker":      cache_entry["ticker"],
                "date":        cache_entry["date"],
                "n_trades":    len(event_trades),
                "event_pf":    event_pf,
                "cvar5_event": cvar5,
                "worst_trade": min(pnls) if pnls else None,
                "dom_exit":    dom,
            })
            chart_ok += 1
        except Exception as exc:
            chart_err += 1
            print(f"  chart error {key}: {exc}", flush=True)

    _write_chart_index(index_rows, chart_dir / "index.html", best_cfg)
    if (chart_ok + chart_err) > 0 and chart_err / (chart_ok + chart_err) > 0.05:
        print(f"*** CHART ERROR RATE {chart_err/(chart_ok+chart_err):.1%} > 5% — review above ***")
    print(f"\nT4 done: {chart_ok} charts, {chart_err} errors → {chart_dir}", flush=True)

    print("\nSub-Phase 2a complete. Outputs:")
    print(f"  sweep_results.json   → {OUT_DIR / 'sweep_results.json'}")
    print(f"  sweep_summary.html   → {OUT_DIR / 'sweep_summary.html'}")
    print(f"  event_charts/        → {chart_dir}")
    print("\nApproval gate: do not begin Sub-Phase 2b until Cooper reviews and approves.")


if __name__ == "__main__":
    main()
