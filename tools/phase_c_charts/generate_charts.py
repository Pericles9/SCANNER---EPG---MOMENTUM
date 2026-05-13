"""Phase C per-event chart runner.

For each event with trades in the CVD-filter results:
  1. Load EventReplay from Phase B replay cache (same tick data, same intensity)
  2. Load TradeData to get trade sizes for dollar-weighted CVD
  3. Build chart with CVD panel replacing I_buy
  4. Write standalone HTML

Writes sortable index.html at the end.

Usage:
    python -m tools.phase_c_charts.generate_charts
    python -m tools.phase_c_charts.generate_charts --tickers KAVL SOUN --dry-run
"""
from __future__ import annotations

import argparse
import html
import json
import time
from pathlib import Path
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.exit_d_tuning.replay import _load_cache
from tools.phase_c_charts.chart import build_chart
from data.loaders.trades import list_events, load_trades

# ── Paths ──────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[2]
_RESULTS_DIR = _ROOT / "results" / "phase_c" / "cvd_filter"
_PHASE_B_CACHE = _ROOT / "results" / "phase_b" / "replay_caches"
_PHASE_U_CACHE = Path(r"D:\Trading Research\hawkes-ofi-impact\results\phase_u\replay_caches")
_PHASE_B_AUDIT = (_ROOT / "results" / "phase_b" / "100_val_seed42"
                  / "event_charts" / "cache_audit.json")
_OUT_DIR = _ROOT / "results" / "phase_c" / "event_charts"
_ERRORS_PATH = _OUT_DIR / "chart_errors.json"

# ── Helpers ────────────────────────────────────────────────────────────────

def _build_mom_pct_map() -> dict[tuple[str, str], float]:
    return {(ev["ticker"], ev["date"]): ev["mom_pct"]
            for ev in list_events(min_mom=0.0, require_date=True)}


def _build_cache_audit_map() -> dict[tuple[str, str], str]:
    if not _PHASE_B_AUDIT.exists():
        return {}
    with open(_PHASE_B_AUDIT) as f:
        audit = json.load(f)
    return {(a["ticker"], a["date"]): a["cache_source"] for a in audit}


def _load_replay(ticker: str, date: str, cache_source: str):
    if cache_source == "phase_b":
        return _load_cache(_PHASE_B_CACHE, ticker, date)
    elif cache_source == "phase_u":
        return _load_cache(_PHASE_U_CACHE, ticker, date)
    # Fallback: try phase_b first, then phase_u
    r = _load_cache(_PHASE_B_CACHE, ticker, date)
    if r is not None:
        return r
    return _load_cache(_PHASE_U_CACHE, ticker, date)


# ── Index builder ──────────────────────────────────────────────────────────

_INDEX_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Phase C CVD Filter — Per-event charts</title>
<style>
  body {{ font-family: ui-monospace, "Cascadia Code", Menlo, Consolas, monospace;
         margin: 24px; color: #222; }}
  h1 {{ font-size: 18px; margin-bottom: 4px; }}
  .meta {{ color: #666; font-size: 13px; margin-bottom: 16px; }}
  table {{ border-collapse: collapse; }}
  th, td {{ padding: 6px 12px; border-bottom: 1px solid #ddd;
            text-align: right; white-space: nowrap; }}
  th {{ cursor: pointer; user-select: none; background: #f4f4f4;
        position: sticky; top: 0; }}
  th:hover {{ background: #e8e8e8; }}
  th .arrow {{ color: #888; font-size: 11px; margin-left: 4px; }}
  td.left, th.left {{ text-align: left; }}
  td.pos {{ color: #0a8; font-weight: 600; }}
  td.neg {{ color: #c33; font-weight: 600; }}
  a {{ color: #0066cc; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<h1>Phase C CVD Filter — Per-event charts</h1>
<div class="meta">{n_events} events with trades &middot; click column header to sort</div>
<table id="t">
  <thead>
    <tr>
      <th class="left" data-col="ticker">Ticker<span class="arrow"></span></th>
      <th class="left" data-col="date">Date<span class="arrow"></span></th>
      <th data-col="n_trades" data-numeric="1">n_trades<span class="arrow"></span></th>
      <th data-col="n_first" data-numeric="1">n_first<span class="arrow"></span></th>
      <th data-col="n_reentry" data-numeric="1">n_reentry<span class="arrow"></span></th>
      <th data-col="n_cvd_blocks" data-numeric="1">cvd_blocked<span class="arrow"></span></th>
      <th data-col="pf" data-numeric="1">event_PF<span class="arrow"></span></th>
      <th data-col="mean_pnl" data-numeric="1">mean_pnl%<span class="arrow"></span></th>
      <th class="left" data-col="session">session<span class="arrow"></span></th>
    </tr>
  </thead>
  <tbody>
{rows}
  </tbody>
</table>
<script>
(function(){{
  var table=document.getElementById('t');
  var tbody=table.querySelector('tbody');
  var headers=table.querySelectorAll('th');
  var sortState={{}};
  function sortBy(idx,numeric,asc){{
    var rows=Array.prototype.slice.call(tbody.querySelectorAll('tr'));
    rows.sort(function(a,b){{
      var av=a.children[idx].getAttribute('data-value');
      var bv=b.children[idx].getAttribute('data-value');
      if(numeric){{av=parseFloat(av);bv=parseFloat(bv);}}
      if(av<bv)return asc?-1:1;
      if(av>bv)return asc?1:-1;
      return 0;
    }});
    rows.forEach(function(r){{tbody.appendChild(r);}});
  }}
  headers.forEach(function(th,idx){{
    if(!th.getAttribute('data-col'))return;
    th.addEventListener('click',function(){{
      var numeric=th.getAttribute('data-numeric')==='1';
      var asc=sortState[th.getAttribute('data-col')]==='asc'?false:true;
      sortState={{}};sortState[th.getAttribute('data-col')]=asc?'asc':'desc';
      headers.forEach(function(h){{var a=h.querySelector('.arrow');if(a)a.textContent='';}});
      var arrow=th.querySelector('.arrow');if(arrow)arrow.textContent=asc?'▲':'▼';
      sortBy(idx,numeric,asc);
    }});
  }});
  // Default sort: PF descending
  var pfTh=table.querySelector('th[data-col="pf"]');
  if(pfTh)pfTh.click();
}})();
</script>
</body>
</html>
"""


def _pf(df: pd.DataFrame) -> float:
    wins = df.loc[df["pnl_pct"] > 0, "pnl_pct"].sum()
    losses = abs(df.loc[df["pnl_pct"] < 0, "pnl_pct"].sum())
    return wins / losses if losses > 0 else float("nan")


def _row_html(ticker: str, date: str, trades_df: pd.DataFrame,
              n_cvd_blocks: int, chart_filename: str) -> str:
    n_t = len(trades_df)
    n_first = int((trades_df["entry_type"] == "first").sum())
    n_reentry = int((trades_df["entry_type"] == "reentry").sum())
    pf_val = _pf(trades_df)
    pf_str = f"{pf_val:.4f}" if not np.isnan(pf_val) else "N/A"
    pf_dv = f"{pf_val:.6f}" if not np.isnan(pf_val) else "0"
    mean_pnl = trades_df["pnl_pct"].mean()
    mean_pnl_str = f"{mean_pnl:+.3f}%"
    mean_pnl_cls = "pos" if mean_pnl > 0 else ("neg" if mean_pnl < 0 else "")
    session = trades_df["session_bucket"].iloc[0] if n_t else ""
    link = html.escape(chart_filename)
    return (
        f'    <tr>'
        f'<td class="left" data-value="{html.escape(ticker)}">'
        f'<a href="{link}">{html.escape(ticker)}</a></td>'
        f'<td class="left" data-value="{html.escape(date)}">{html.escape(date)}</td>'
        f'<td data-value="{n_t}">{n_t}</td>'
        f'<td data-value="{n_first}">{n_first}</td>'
        f'<td data-value="{n_reentry}">{n_reentry}</td>'
        f'<td data-value="{n_cvd_blocks}">{n_cvd_blocks}</td>'
        f'<td data-value="{pf_dv}">{pf_str}</td>'
        f'<td data-value="{mean_pnl:.6f}" class="{mean_pnl_cls}">{mean_pnl_str}</td>'
        f'<td class="left" data-value="{html.escape(session)}">{html.escape(session)}</td>'
        f'</tr>'
    )


def _write_index(rows_html: list[str], n_events: int, out_path: Path) -> None:
    content = _INDEX_TEMPLATE.format(
        n_events=n_events,
        rows="\n".join(rows_html),
    )
    out_path.write_text(content, encoding="utf-8")


# ── Main ───────────────────────────────────────────────────────────────────

def main(tickers: list[str] | None = None, dry_run: bool = False) -> None:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)

    with open(_RESULTS_DIR / "per_event_summary.json") as f:
        all_events = json.load(f)

    per_trade = pd.read_parquet(_RESULTS_DIR / "per_trade.parquet")
    trade_event_keys = set(zip(per_trade["ticker"], per_trade["date"]))

    mom_pct_map = _build_mom_pct_map()
    audit_map = _build_cache_audit_map()

    # Events with trades
    chartable = [e for e in all_events
                 if (e["ticker"], e["date"]) in trade_event_keys]
    if tickers:
        chartable = [e for e in chartable if e["ticker"] in tickers]

    n_total = len(chartable)
    print(f"Phase C CVD chart runner — {n_total} events -> {_OUT_DIR}/")
    print()

    errors = []
    index_rows: list[str] = []
    n_written = 0
    total_start = time.time()

    for idx, ev in enumerate(chartable, 1):
        ticker = ev["ticker"]
        date = ev["date"]
        n_cvd_blocks = ev.get("n_cvd_blocks", 0)
        ev_start = time.time()

        print(f"[{idx}/{n_total}] {ticker} {date}", end="", flush=True)

        if dry_run:
            print(" [DRY RUN]")
            continue

        out_path = _OUT_DIR / f"{ticker}_{date}.html"
        chart_filename = f"{ticker}_{date}.html"

        try:
            # Load replay
            cache_source = audit_map.get((ticker, date), "fallback")
            replay = _load_replay(ticker, date, cache_source)
            if replay is None:
                raise FileNotFoundError(
                    f"No replay cache for {ticker} {date} (source={cache_source})"
                )

            # Load trade sizes
            mom_pct = mom_pct_map.get((ticker, date))
            if mom_pct is None:
                raise KeyError(f"mom_pct not found in catalog for {ticker} {date}")
            td = load_trades(ticker, date, mom_pct)
            sizes = td.sizes.astype(np.float64)

            if len(sizes) != len(replay.timestamps_ns):
                raise ValueError(
                    f"TradeData size mismatch: td.n={len(sizes)} "
                    f"replay.n={len(replay.timestamps_ns)}"
                )

            trades_slice = per_trade[
                (per_trade["ticker"] == ticker) & (per_trade["date"] == date)
            ].copy()

            fig = build_chart(ticker, date, replay, sizes, trades_slice, ev)
            fig.write_html(str(out_path), include_plotlyjs="cdn")

            elapsed = time.time() - ev_start
            n_written += 1
            print(f"  OK  {elapsed:.1f}s")

            index_rows.append(_row_html(ticker, date, trades_slice,
                                        n_cvd_blocks, chart_filename))

        except Exception as exc:
            elapsed = time.time() - ev_start
            import traceback
            print(f"  ERROR  {elapsed:.1f}s  {exc}")
            errors.append({
                "ticker": ticker, "date": date,
                "error": str(exc),
                "elapsed_sec": round(elapsed, 2),
                "traceback": traceback.format_exc(),
            })

    total_elapsed = time.time() - total_start

    with open(_ERRORS_PATH, "w") as f:
        json.dump(errors, f, indent=2)

    if not dry_run:
        _write_index(index_rows, n_written, _OUT_DIR / "index.html")
        print(f"\nindex.html written ({n_written} events)")

    print(f"\nDone. {n_written}/{n_total} charts | {len(errors)} errors | "
          f"{total_elapsed:.1f}s total")
    if errors:
        print("Errors:")
        for e in errors:
            print(f"  {e['ticker']} {e['date']}: {e['error']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase C per-event chart runner")
    parser.add_argument("--tickers", nargs="+", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(tickers=args.tickers, dry_run=args.dry_run)
