"""
T4 — EPG v2 WJI Diagnostic Charts

For each event in the 100-event stratified val sample (seed=42, split=val):
  1. Load trades + quotes
  2. Run Hawkes online refit (same config as runner.py)
  3. Replay gate in background mode, capturing WJI internals per tick
  4. Generate 2-panel chart:
     Panel 1: 10s OHLCV candlesticks, T_event / warmup_end markers
     Panel 2: WJI panel —
       - live WJI (blue solid)
       - p_open x decaying peak_WJI (dashed orange) -- peak-based threshold
       - WJI_background(t) (dotted grey step) -- sqrt(mu_buy/(mu_buy+mu_sell))
       - candidate C x WJI_bg floor lines for C in CANDIDATE_C_VALUES
       - PASS shading (light green), WARMUP shading (light grey)

Usage:
    cd d:/Trading Research/scanner-epg-momentum
    python -m backtest.tools.phase_wji_v2.t4_diag_charts

Output:
    results/phase_wji_v2/t4_diag/charts/{TICKER}_{DATE}.html
    results/phase_wji_v2/t4_diag/index.html
"""
from __future__ import annotations

import json
import logging
import math
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from data.loaders.trades import (
    load_trades, list_events, _session_ns_bounds,
    compute_lambda_ref_per_event,
)
from data.loaders.quotes import load_quotes
from data.schemas.mom_db import CONFIG_DIR, NS_PER_SECOND
from core.epg.anchor import EventAnchor
from core.epg.gate import ParticipationGate, GateState
from core.ofi.trade_ofi import compute_trade_ofi
from backtest.runner import _hawkes_replay_with_refit

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

# ── Constants (must match runner.py) ─────────────────────────────────────────
EPG_K = 5
EPG_TAU = 300.0
EPG_P = 0.65
EPG_WARMUP = 300.0
OHLC_BIN_SEC = 10.0
SPLIT = "val"
RANDOM_SAMPLE = 100
SEED = 42
TAU_PEAK = 600.0
C_PLACEHOLDER = 1.5

# Candidate C values shown as floor lines for Cooper to choose from
CANDIDATE_C_VALUES = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
C_COLORS = {
    0.5:  "rgba(120,200,120,0.50)",
    1.0:  "rgba(30,144,255,0.50)",
    1.5:  "rgba(255,140,0,0.80)",   # highlighted: current placeholder
    2.0:  "rgba(220,50,50,0.50)",
    2.5:  "rgba(160,0,0,0.50)",
    3.0:  "rgba(128,0,128,0.50)",
}

OUT_DIR = REPO_ROOT / "results" / "phase_wji_v2" / "t4_diag" / "charts"


# ══════════════════════════════════════════════════════════════════════════════
#  Event loader (replicates runner.py stratified sampling exactly)
# ══════════════════════════════════════════════════════════════════════════════

def load_val_sample(boundary: dict) -> list[dict]:
    val_start = boundary["val_split_start_date"]
    test_start = boundary["test_split_start_date"]

    all_events = list_events(min_mom=50.0, require_date=True)
    events = [e for e in all_events if val_start <= e["date"] < test_start]

    rng = random.Random(SEED)
    by_year: dict[str, list] = {}
    for e in events:
        by_year.setdefault(e["date"][:4], []).append(e)

    year_counts = {y: len(evs) for y, evs in by_year.items()}
    total = sum(year_counts.values())
    alloc = {y: int(RANDOM_SAMPLE * cnt / total) for y, cnt in year_counts.items()}
    remainder = RANDOM_SAMPLE - sum(alloc.values())
    for y in sorted(year_counts, key=year_counts.get, reverse=True):
        if remainder <= 0:
            break
        alloc[y] += 1
        remainder -= 1

    sampled = []
    for y in sorted(by_year):
        n_y = min(alloc[y], len(by_year[y]))
        sampled.extend(rng.sample(by_year[y], n_y))

    return sorted(sampled, key=lambda e: (e["date"], e["ticker"]))


# ══════════════════════════════════════════════════════════════════════════════
#  Per-event worker (runs in subprocess)
# ══════════════════════════════════════════════════════════════════════════════

def _worker(args: dict) -> dict:
    """Compute WJI debug arrays for one event, save chart, return metadata."""
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT))

    ticker = args["ticker"]
    date = args["date"]
    mom_pct = args["mom_pct"]
    fp = args["fp"]
    q_bar_cfg = args["q_bar_cfg"]
    out_dir = Path(args["out_dir"])

    try:
        td = load_trades(ticker, date, mom_pct)
        if td is None or td.n_trades < 30:
            return {"ticker": ticker, "date": date, "status": "skipped", "reason": "insufficient_trades"}

        qd = load_quotes(ticker, date, mom_pct)
        if qd is None or qd.n_quotes < 10:
            return {"ticker": ticker, "date": date, "status": "skipped", "reason": "insufficient_quotes"}

        N = td.n_trades

        # Lee-Ready sides
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

        # Hawkes online refit
        rho = fp.get("rho", 0.99)
        global_lambda_ref = fp["mu_buy"] + fp["mu_sell"]
        per_event_lref = compute_lambda_ref_per_event(ticker, date)
        lambda_ref = (
            per_event_lref
            if not math.isnan(per_event_lref) and per_event_lref > 0
            else global_lambda_ref
        )

        lam_buy_out = np.zeros(N, dtype=np.float64)
        lam_sell_out = np.zeros(N, dtype=np.float64)
        E_out = np.zeros(N, dtype=np.float64)
        Edot_out = np.zeros(N, dtype=np.float64)
        n_base_out = np.zeros(N, dtype=np.float64)
        dv_arr = td.prices.astype(np.float64) * td.sizes.astype(np.float64)
        mu_buy_out = np.zeros(N, dtype=np.float64)
        mu_sell_out = np.zeros(N, dtype=np.float64)
        dbar_out = np.zeros(N, dtype=np.float64)

        _hawkes_replay_with_refit(
            t_sec=td.t_sec, sides=sides, rho=rho, lambda_ref=lambda_ref,
            init_params=fp, rho_E=rho,
            lam_buy_out=lam_buy_out, lam_sell_out=lam_sell_out,
            E_out=E_out, Edot_out=Edot_out, n_base_out=n_base_out,
            dv_arr=dv_arr, mu_buy_out=mu_buy_out,
            mu_sell_out=mu_sell_out, dbar_out=dbar_out,
        )

        # EPG anchor + background gate
        lambda_ref_epg = fp["mu_buy"] + fp["mu_sell"]
        lambda_hat = lam_buy_out + lam_sell_out

        # Pre-pass: find t_event so lambda_v_ref can be computed before the gate activates
        anchor_pre = EventAnchor(lambda_ref=lambda_ref_epg, k_multiplier=EPG_K)
        t_event_pre: Optional[float] = None
        for i in range(N):
            t_ev = anchor_pre.update(float(lambda_hat[i]), float(td.t_sec[i]))
            if t_ev is not None:
                t_event_pre = t_ev
                break

        # Compute lambda_v_ref = pre-event mean of lambda_V EMA [session_start, T_event)
        # Uses same half-life as the gate (EPG_TAU=300s) to keep units consistent.
        LN2 = math.log(2.0)
        _decay_rate_v = LN2 / EPG_TAU
        _lv = 0.0
        _last_t_v: Optional[float] = None
        _pre_event_lambdas: list[float] = []
        for i in range(N):
            t = float(td.t_sec[i])
            dv = float(dv_arr[i])
            if _last_t_v is None:
                _lv = dv * _decay_rate_v
            else:
                _dt = max(0.0, t - _last_t_v)
                _lv = _lv * math.exp(-_decay_rate_v * _dt) + dv * _decay_rate_v
            _last_t_v = t
            if t_event_pre is None or t < t_event_pre:
                _pre_event_lambdas.append(_lv)
            else:
                break
        lambda_v_ref = (
            max(sum(_pre_event_lambdas) / len(_pre_event_lambdas), 1e-9)
            if _pre_event_lambdas else 1e-9
        )
        mu_buy_ref = max(float(fp["mu_buy"]), 1e-9)

        anchor = EventAnchor(lambda_ref=lambda_ref_epg, k_multiplier=EPG_K)

        gate = ParticipationGate(
            half_life_seconds=EPG_TAU,
            peak_threshold_p=EPG_P,
            warmup_seconds=EPG_WARMUP,
            gate_mode="background",
            tau_peak=TAU_PEAK,
            C=C_PLACEHOLDER,
        )

        wji_arr = np.zeros(N, dtype=np.float64)
        wji_bg_arr = np.zeros(N, dtype=np.float64)
        p_peak_arr = np.zeros(N, dtype=np.float64)
        gate_state_arr: list[str] = []
        t_event: Optional[float] = None
        t_event_fired = False

        for i in range(N):
            t = float(td.t_sec[i])
            t_ev = anchor.update(float(lambda_hat[i]), t)
            if t_ev is not None and not t_event_fired:
                t_event_fired = True
                t_event = t_ev
                gate.activate(t_event, lambda_v_ref=lambda_v_ref, mu_buy_ref=mu_buy_ref)

            if gate._active:
                state = gate.update(float(dv_arr[i]), t, side=int(sides[i]))
                dbg = gate.last_bg_debug
                if dbg:
                    wji_arr[i] = dbg.get("wji", 0.0)
                    wji_bg_arr[i] = dbg.get("wji_bg", 0.0)
                    p_peak_arr[i] = dbg.get("p_peak", 0.0)
            else:
                state = GateState.INACTIVE

            gate_state_arr.append(state.value)

        pass_count = sum(1 for s in gate_state_arr if s == "PASS")
        pass_pct = 100.0 * pass_count / N if N > 0 else 0.0

        # Build and write chart
        try:
            import plotly.graph_objects as go
            from plotly.subplots import make_subplots as _msp

            t_arr = td.t_sec
            prices = td.prices
            t_warmup_end = (t_event + EPG_WARMUP) if t_event is not None else None

            fig = _msp(
                rows=2, cols=1, shared_xaxes=True,
                row_heights=[0.40, 0.60], vertical_spacing=0.04,
                subplot_titles=[
                    f"{ticker}  {date}  (+{mom_pct:.1f}%)",
                    (
                        f"WJI gate (background)  tau_peak={TAU_PEAK:.0f}s  "
                        f"C={C_PLACEHOLDER} [placeholder]  "
                        f"pass={pass_pct:.1f}%  thin={gate.thin_guard_rate:.3f}"
                    ),
                ],
            )

            # Panel 1: OHLCV
            bar_t, bar_o, bar_h, bar_l, bar_c = _ticks_to_ohlc(t_arr, prices)
            if bar_t:
                fig.add_trace(go.Candlestick(
                    x=bar_t, open=bar_o, high=bar_h, low=bar_l, close=bar_c,
                    name="Price", showlegend=False,
                    increasing_line_color="#2ca02c", decreasing_line_color="#d62728",
                ), row=1, col=1)

            if t_event is not None:
                fig.add_vline(x=float(t_event), line_dash="dash", line_color="orange",
                              annotation_text="T_event", annotation_position="top left",
                              row=1, col=1)
            if t_warmup_end is not None and float(t_warmup_end) <= float(t_arr[-1]):
                fig.add_vline(x=float(t_warmup_end), line_dash="dot", line_color="grey",
                              annotation_text="warmup_end", annotation_position="top left",
                              row=1, col=1)

            # Panel 2: WJI
            _add_state_shading_str(fig, t_arr, gate_state_arr, "PASS",
                                   "rgba(0,180,0,0.12)", 2)
            _add_state_shading_str(fig, t_arr, gate_state_arr, "WARMUP",
                                   "rgba(180,180,180,0.12)", 2)

            for c_val in CANDIDATE_C_VALUES:
                floor = wji_bg_arr * c_val
                width = 2.0 if c_val == C_PLACEHOLDER else 1.0
                dash = "solid" if c_val == C_PLACEHOLDER else "dot"
                name = f"C={c_val} [placeholder]" if c_val == C_PLACEHOLDER else f"C={c_val}"
                fig.add_trace(go.Scatter(
                    x=list(t_arr), y=list(floor), mode="lines",
                    line=dict(color=C_COLORS[c_val], width=width, dash=dash), name=name,
                ), row=2, col=1)

            fig.add_trace(go.Scatter(
                x=list(t_arr), y=list(wji_bg_arr), mode="lines",
                line=dict(color="rgba(100,100,100,0.5)", width=1, dash="dot"),
                name="WJI_bg",
            ), row=2, col=1)

            fig.add_trace(go.Scatter(
                x=list(t_arr), y=list(p_peak_arr), mode="lines",
                line=dict(color="rgba(200,100,0,0.7)", width=1.5, dash="dash"),
                name=f"p_open×peak ({EPG_P})",
            ), row=2, col=1)

            fig.add_trace(go.Scatter(
                x=list(t_arr), y=list(wji_arr), mode="lines",
                line=dict(color="#1f77b4", width=1.5), name="WJI",
            ), row=2, col=1)

            if t_event is not None:
                fig.add_vline(x=float(t_event), line_dash="dash", line_color="orange",
                              annotation_text="T_event", annotation_position="top left",
                              row=2, col=1)
            if t_warmup_end is not None and float(t_warmup_end) <= float(t_arr[-1]):
                fig.add_vline(x=float(t_warmup_end), line_dash="dot", line_color="grey",
                              annotation_text="warmup_end", annotation_position="top left",
                              row=2, col=1)

            fig.update_layout(
                height=760, template="plotly_white",
                title=(
                    f"T4 WJI Diagnostic — {ticker} {date}  "
                    f"tau_peak={TAU_PEAK:.0f}s  C={C_PLACEHOLDER} [placeholder]"
                ),
                hovermode="x unified",
                margin=dict(l=60, r=20, t=80, b=40),
                xaxis_rangeslider_visible=False,
                legend=dict(orientation="h", x=0.01, y=-0.05, font=dict(size=10)),
            )
            fig.update_yaxes(title_text="Price ($)", row=1, col=1)
            fig.update_yaxes(title_text="WJI", row=2, col=1)
            fig.update_xaxes(title_text="Seconds since session start", row=2, col=1)

            out_dir.mkdir(parents=True, exist_ok=True)
            fname = f"{ticker}_{date}.html"
            fig.write_html(str(out_dir / fname), include_plotlyjs="cdn", full_html=True)

        except Exception as chart_err:
            import traceback
            return {
                "ticker": ticker, "date": date, "status": "chart_error",
                "error": str(chart_err), "traceback": traceback.format_exc(),
            }

        return {
            "ticker": ticker,
            "date": date,
            "mom_pct": mom_pct,
            "status": "ok",
            "pass_pct": pass_pct,
            "thin_guard_rate": gate.thin_guard_rate,
            "filename": f"{ticker}_{date}.html",
        }

    except Exception as exc:
        import traceback
        return {
            "ticker": ticker, "date": date, "status": "error",
            "error": str(exc), "traceback": traceback.format_exc(),
        }


def _ticks_to_ohlc(t_sec, prices, bin_sec: float = OHLC_BIN_SEC):
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


def _add_state_shading_str(fig, t, state_strs, target: str, fill_color: str, row: int):
    in_region = False
    region_start = None
    for i, s in enumerate(state_strs):
        if s == target and not in_region:
            in_region = True
            region_start = float(t[i])
        elif s != target and in_region:
            in_region = False
            fig.add_vrect(x0=region_start, x1=float(t[i]),
                          fillcolor=fill_color, layer="below", line_width=0, row=row, col=1)
    if in_region:
        fig.add_vrect(x0=region_start, x1=float(t[-1]),
                      fillcolor=fill_color, layer="below", line_width=0, row=row, col=1)


# ══════════════════════════════════════════════════════════════════════════════
#  Index HTML
# ══════════════════════════════════════════════════════════════════════════════

def build_index_html(chart_rows: list[dict], n_skipped: int, n_errors: int) -> str:
    rows_html = ""
    for r in sorted(chart_rows, key=lambda x: (x["date"], x["ticker"])):
        rows_html += (
            f"<tr>"
            f"<td>{r['ticker']}</td>"
            f"<td>{r['date']}</td>"
            f"<td>{r['mom_pct']:.1f}%</td>"
            f"<td>{r['pass_pct']:.1f}%</td>"
            f"<td>{r['thin_guard_rate']:.3f}</td>"
            f"<td><a href='charts/{r['filename']}' target='_blank'>chart</a></td>"
            f"</tr>\n"
        )

    return f"""<!DOCTYPE html>
<html><head>
<title>T4 WJI Diagnostic Charts &mdash; Phase WJI-v2</title>
<style>
  body {{ font-family: monospace; padding: 20px; }}
  h2 {{ margin-bottom: 4px; }}
  p {{ margin: 2px 0 12px 0; color: #555; font-size: 0.9em; }}
  table {{ border-collapse: collapse; }}
  td, th {{ border: 1px solid #ccc; padding: 4px 8px; }}
  th {{ cursor: pointer; background: #f0f0f0; user-select: none; }}
  tr:hover {{ background: #f8f8ff; }}
</style>
</head><body>
<h2>T4 &mdash; EPG v2 WJI Diagnostic Charts</h2>
<p>
  100-event val sample (seed={SEED}) &middot;
  gate_mode=background &middot;
  tau_peak={TAU_PEAK:.0f}s &middot;
  <strong>C={C_PLACEHOLDER} [placeholder &mdash; Cooper selects from charts]</strong> &middot;
  {len(chart_rows)} charts, {n_skipped} skipped, {n_errors} errors
</p>
<p><strong>Candidate C floor lines shown:</strong>
{' / '.join(
    f'<span style="font-weight:{"bold" if c == C_PLACEHOLDER else "normal"}">C={c}</span>'
    for c in CANDIDATE_C_VALUES
)} &mdash; orange solid = current placeholder (C=1.5)</p>
<table id="t">
<thead><tr>
  <th onclick="sortTable(0)">Ticker</th>
  <th onclick="sortTable(1)">Date</th>
  <th onclick="sortTable(2)">Mom%</th>
  <th onclick="sortTable(3)">PASS%</th>
  <th onclick="sortTable(4)">Thin guard</th>
  <th>Chart</th>
</tr></thead>
<tbody>
{rows_html}
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


# ══════════════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    if not HAS_PLOTLY:
        log.error("plotly not installed -- run: pip install plotly")
        sys.exit(1)

    with open(CONFIG_DIR / "holdout_boundary.json") as f:
        boundary = json.load(f)
    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_median = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    # Per-event Hawkes params (Phase A fitted, fallback to median)
    phase_a_path = REPO_ROOT / "results" / "phase_a" / "production_fit_results.json"
    per_event_params: dict[tuple, dict] = {}
    if phase_a_path.exists():
        with open(phase_a_path) as f:
            for r in json.load(f):
                if r.get("status") == "success" and "final_params" in r:
                    per_event_params[(r["ticker"], r["date"])] = r["final_params"]

    events = load_val_sample(boundary)
    log.info("T4: %d events (seed=%d, split=%s)", len(events), SEED, SPLIT)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    work_items = []
    for ev in events:
        fp = per_event_params.get((ev["ticker"], ev["date"]), hawkes_median)
        work_items.append({
            "ticker": ev["ticker"],
            "date": ev["date"],
            "mom_pct": ev["mom_pct"],
            "fp": fp,
            "q_bar_cfg": q_bar_cfg,
            "out_dir": str(OUT_DIR),
        })

    chart_rows: list[dict] = []
    n_skipped = 0
    n_errors = 0
    t0 = time.time()

    import os
    n_workers = min(os.cpu_count() or 4, len(work_items))
    log.info("Running with %d workers", n_workers)

    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_worker, item): item for item in work_items}
        done = 0
        for future in as_completed(futures):
            done += 1
            item = futures[future]
            key = f"{item['ticker']} {item['date']}"
            try:
                r = future.result()
            except Exception as exc:
                import traceback
                log.error("[%d/%d] %s EXCEPTION: %s", done, len(work_items), key, exc)
                n_errors += 1
                continue

            if r["status"] == "ok":
                chart_rows.append(r)
                log.info(
                    "[%d/%d] %s  pass=%.1f%%  thin=%.3f",
                    done, len(work_items), key, r["pass_pct"], r["thin_guard_rate"],
                )
            elif r["status"] == "skipped":
                log.info("[%d/%d] %s skipped: %s", done, len(work_items), key, r.get("reason"))
                n_skipped += 1
            else:
                log.error("[%d/%d] %s error: %s", done, len(work_items), key, r.get("error"))
                n_errors += 1

    # Write index
    index_path = OUT_DIR.parent / "index.html"
    index_path.write_text(
        build_index_html(chart_rows, n_skipped, n_errors), encoding="utf-8"
    )

    log.info(
        "\nT4 complete: %d charts, %d skipped, %d errors in %.1fs",
        len(chart_rows), n_skipped, n_errors, time.time() - t0,
    )
    log.info("Index: %s", index_path)


if __name__ == "__main__":
    main()
