"""Generate per-event 4-panel charts from an EPG-Rapid R0 baseline run.

Reads per_trade.json from --results-dir.  For each traded event, re-derives
the signal stack (fixed-beta Hawkes, setup filter, EPG gate) and builds a
4-panel chart saved as HTML.  Writes a sortable index.html.

Usage:
    python -m backtest.tools.phase_r0.run_r0_charts \\
        --results-dir backtest/results/phase_r0/baseline \\
        --out-dir backtest/results/phase_r0/charts
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from data.schemas.mom_db import CONFIG_DIR, NS_PER_SECOND
from data.loaders.trades import load_trades, list_events, _session_ns_bounds
from data.loaders.quotes import load_quotes
from core.ofi.trade_ofi import compute_trade_ofi
from core.epg.anchor import EventAnchor
from core.epg.gate import ParticipationGate
from core.hawkes.engine import hawkes_replay_fixed_beta
from backtest.setup_filter import run_setup_filter, _build_1min_bars
from tools.phase_r0.chart import build_chart


# ── EPG / Hawkes constants (mirrors runner_rapid.py) ────────────────────
EPG_K = 5
EPG_TAU = 300.0
EPG_P = 0.65
EPG_WARMUP = 300.0


def _compute_signals(td, qd, fp, rho_E, epg_cfg, session_start_ns, session_end_ns):
    """Return (sf, bar_starts, lam_buy, lam_sell, epg_state_ints, t_event_ns)."""
    N = td.n_trades

    # Setup filter
    sf = run_setup_filter(
        timestamps=td.timestamps,
        prices=td.prices,
        sizes=td.sizes,
        session_start_ns=session_start_ns,
        session_end_ns=session_end_ns,
    )

    # Bar starts (same call as setup_filter uses internally)
    _, _, _, _, _, _, bar_starts = _build_1min_bars(
        td.timestamps, td.prices,
        td.sizes.astype(np.int64),
        session_start_ns, session_end_ns,
    )

    # Lee-Ready sides (needed for Hawkes)
    tier_qbar = 250.0  # wide tier fallback
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

    # Hawkes fixed-beta replay (approximate — no online refit)
    lam_buy = np.zeros(N, dtype=np.float64)
    lam_sell = np.zeros(N, dtype=np.float64)
    E_out = np.zeros(N, dtype=np.float64)
    Edot_out = np.zeros(N, dtype=np.float64)
    hawkes_replay_fixed_beta(
        td.t_sec, sides,
        fp["alpha_buy_self"], 0.0,
        fp["alpha_sell_self"], 0.0,
        fp["mu_buy"], fp["mu_sell"],
        fp["beta"], rho_E,
        lam_buy, lam_sell, E_out, Edot_out,
    )
    lambda_hat = lam_buy + lam_sell

    # EPG gate (approximate, matches fixed-beta lambda_hat)
    gate_mode = epg_cfg.get("gate_mode", "peak")
    tau_peak = epg_cfg.get("tau_peak", 600.0)
    C = epg_cfg.get("C", 1.5)
    lambda_ref = fp["mu_buy"] + fp["mu_sell"]
    anchor = EventAnchor(lambda_ref=lambda_ref, k_multiplier=EPG_K)
    gate = ParticipationGate(
        half_life_seconds=EPG_TAU,
        peak_threshold_p=EPG_P,
        warmup_seconds=EPG_WARMUP,
        gate_mode=gate_mode,
        tau_peak=tau_peak,
        C=C,
    )

    epg_state_ints = np.zeros(N, dtype=np.int32)
    t_event_ns: int | None = None
    t_event_fired = False
    for i in range(N):
        t_ev = anchor.update(lambda_hat[i], td.t_sec[i])
        if t_ev is not None and not t_event_fired:
            gate.activate(t_ev)
            t_event_fired = True
            t_event_ns = int(td.timestamps[i])
        dv = float(td.prices[i]) * float(td.sizes[i])
        epg_state_ints[i] = int(gate.update(dv, td.t_sec[i]).value)

    return sf, bar_starts, lam_buy, lam_sell, epg_state_ints, t_event_ns


def _build_index_html(rows: list[dict], out_dir: Path) -> None:
    """Write sortable index.html linking to per-event chart files."""
    sorted_rows = sorted(rows, key=lambda r: r["pnl_pct"])

    def _fmt(v):
        return f"{v:.3f}" if isinstance(v, float) else str(v)

    table_rows = ""
    for rank, row in enumerate(sorted_rows, 1):
        pnl = row["pnl_pct"]
        pnl_style = "color:#00CC44" if pnl > 0 else ("color:#CC2200" if pnl < 0 else "")
        table_rows += (
            f"<tr>"
            f"<td>{rank}</td>"
            f"<td>{row['ticker']}</td>"
            f"<td>{row['date']}</td>"
            f"<td style='{pnl_style}'>{pnl:.3f}%</td>"
            f"<td>{row['hold_sec']:.0f}s</td>"
            f"<td>{row['exit_reason']}</td>"
            f"<td><a href='{row['chart_file']}' target='_blank'>chart</a></td>"
            f"</tr>\n"
        )

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>EPG-Rapid R0 — {len(rows)} events</title>
<style>
  body {{ font-family: monospace; background:#111; color:#ddd; padding:20px; }}
  table {{ border-collapse: collapse; width:100%; }}
  th, td {{ border:1px solid #333; padding:6px 10px; text-align:left; }}
  th {{ background:#222; cursor:pointer; user-select:none; }}
  th:hover {{ background:#333; }}
  tr:nth-child(even) {{ background:#1a1a1a; }}
  tr:hover {{ background:#2a2a2a; }}
  a {{ color:#5588ff; }}
  .summary {{ margin-bottom:16px; color:#aaa; font-size:13px; }}
</style>
</head>
<body>
<h2>EPG-Rapid R0 — {len(rows)} traded events</h2>
<p class="summary">Sorted by PnL% ascending (worst to best). Click column headers to re-sort.</p>
<table id="tbl">
<thead>
<tr>
  <th onclick="sort(0)">Rank</th>
  <th onclick="sort(1)">Ticker</th>
  <th onclick="sort(2)">Date</th>
  <th onclick="sort(3)">PnL%</th>
  <th onclick="sort(4)">Hold(s)</th>
  <th onclick="sort(5)">Exit Reason</th>
  <th>Chart</th>
</tr>
</thead>
<tbody>
{table_rows}
</tbody>
</table>
<script>
let _lastCol = -1, _asc = true;
function sort(col) {{
  const tbody = document.querySelector('#tbl tbody');
  const rows = Array.from(tbody.querySelectorAll('tr'));
  _asc = (_lastCol === col) ? !_asc : true;
  _lastCol = col;
  rows.sort((a, b) => {{
    const av = a.cells[col].textContent.trim().replace('%','').replace('s','');
    const bv = b.cells[col].textContent.trim().replace('%','').replace('s','');
    const an = parseFloat(av), bn = parseFloat(bv);
    const cmp = isNaN(an) || isNaN(bn) ? av.localeCompare(bv) : an - bn;
    return _asc ? cmp : -cmp;
  }});
  rows.forEach(r => tbody.appendChild(r));
}}
</script>
</body>
</html>
"""
    (out_dir / "index.html").write_text(html, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Generate R0 per-event charts")
    parser.add_argument("--results-dir", required=True,
                        help="Dir containing per_trade.json from T4 baseline run")
    parser.add_argument("--out-dir", default=None,
                        help="Output dir for HTML charts (default: <results-dir>/charts)")
    parser.add_argument("--n-hold", type=int, default=15,
                        help="n_hold used in T4 baseline (for entry-eligible shading)")
    parser.add_argument("--config", default=None,
                        help="Path to strategy.json (default: backtest/config/strategy.json)")
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    out_dir = Path(args.out_dir) if args.out_dir else results_dir / "charts"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Load configs
    config_path = (Path(args.config) if args.config
                   else Path(__file__).resolve().parents[3] / "config" / "strategy.json")
    with open(config_path) as f:
        strategy_cfg = json.load(f)
    with open(CONFIG_DIR / "hawkes_params.json") as f:
        fp = json.load(f)

    rho_E = fp.get("rho", 0.99)
    epg_cfg = strategy_cfg.get("epg", {})

    # Build mom_pct lookup from event catalog (mom_pct not stored in per_trade.json)
    all_events = list_events(min_mom=50.0, require_date=True)
    mom_pct_lookup: dict[tuple, float] = {
        (e["ticker"], e["date"]): e["mom_pct"] for e in all_events
    }

    # Load trades
    trades_path = results_dir / "per_trade.json"
    if not trades_path.exists():
        print(f"ERROR: {trades_path} not found — run T4 baseline first")
        sys.exit(1)
    with open(trades_path) as f:
        all_trades = json.load(f)

    if not all_trades:
        print("No trades found in per_trade.json")
        sys.exit(0)

    # Group by event (ticker, date) — one trade per event in T4 (hard re-entry off)
    event_trades: dict[tuple, dict] = {}
    for t in all_trades:
        key = (t["ticker"], t["date"])
        event_trades[key] = t  # last wins; there should only be one per event in T4

    print(f"Found {len(event_trades)} unique events to chart")

    index_rows = []
    n_done = 0
    n_err = 0

    for (ticker, date), trade in sorted(event_trades.items()):
        try:
            mom_pct = mom_pct_lookup.get((ticker, date), 50.0)

            td = load_trades(ticker, date, mom_pct)
            if td is None:
                print(f"  SKIP {ticker} {date}: no trade data")
                n_err += 1
                continue

            qd = load_quotes(ticker, date, mom_pct)
            if qd is None:
                print(f"  SKIP {ticker} {date}: no quote data")
                n_err += 1
                continue

            session_start_ns, session_end_ns = _session_ns_bounds(date)

            sf, bar_starts, lam_buy, lam_sell, epg_state_ints, t_event_ns = _compute_signals(
                td, qd, fp, rho_E, epg_cfg, session_start_ns, session_end_ns,
            )

            fig = build_chart(
                ticker=ticker,
                date=date,
                timestamps_ns=td.timestamps,
                prices=td.prices,
                sf=sf,
                bar_starts=bar_starts,
                lam_buy=lam_buy,
                lam_sell=lam_sell,
                epg_state_ints=epg_state_ints,
                t_event_ns=t_event_ns,
                trade=trade,
                n_hold=args.n_hold,
            )

            chart_fname = f"{ticker}_{date}.html"
            fig.write_html(str(out_dir / chart_fname), include_plotlyjs="cdn")

            index_rows.append({
                "ticker": ticker,
                "date": date,
                "pnl_pct": float(trade["pnl_pct"]),
                "hold_sec": float(trade.get("hold_sec", 0.0)),
                "exit_reason": str(trade.get("exit_reason", "")),
                "chart_file": chart_fname,
            })

            n_done += 1
            if n_done % 10 == 0:
                print(f"  {n_done}/{len(event_trades)} done...")

        except Exception as exc:
            import traceback
            print(f"  ERROR {ticker} {date}: {exc}")
            traceback.print_exc()
            n_err += 1

    _build_index_html(index_rows, out_dir)
    print(f"\nDone: {n_done} charts written, {n_err} errors")
    print(f"Index: {out_dir / 'index.html'}")


if __name__ == "__main__":
    main()
