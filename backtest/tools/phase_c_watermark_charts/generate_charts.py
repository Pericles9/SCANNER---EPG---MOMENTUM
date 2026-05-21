"""Phase C watermark filter per-event chart runner.

For each event with trades in the watermark_0.05 results:
  1. Load EventReplay from Phase B replay cache (same tick data, same intensity)
  2. Build chart with watermark drawdown panel (Panel 3)
  3. Write standalone HTML to results/phase_c/event_charts_watermark/

Writes sortable index.html at the end.

Usage:
    python -m tools.phase_c_watermark_charts.generate_charts
    python -m tools.phase_c_watermark_charts.generate_charts --tickers SVRE XBP --dry-run
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
from tools.phase_c_watermark_charts.chart import build_chart

# ── Paths ──────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[2]
_RESULTS_DIR = _ROOT / "results" / "phase_c" / "watermark_0.05"
_PHASE_B_CACHE = _ROOT / "results" / "phase_b" / "replay_caches"
_PHASE_U_CACHE = Path(r"D:\Trading Research\hawkes-ofi-impact\results\phase_u\replay_caches")
_PHASE_B_AUDIT = (_ROOT / "results" / "phase_b" / "100_val_seed42"
                  / "event_charts" / "cache_audit.json")
_OUT_DIR = _ROOT / "results" / "phase_c" / "event_charts_watermark"
_ERRORS_PATH = _OUT_DIR / "chart_errors.json"

_WATERMARK_THRESHOLD = 0.05


# ── Helpers ────────────────────────────────────────────────────────────────

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
    r = _load_cache(_PHASE_B_CACHE, ticker, date)
    if r is not None:
        return r
    return _load_cache(_PHASE_U_CACHE, ticker, date)


def _pf(df: pd.DataFrame) -> float:
    wins = df.loc[df["pnl_pct"] > 0, "pnl_pct"].sum()
    losses = abs(df.loc[df["pnl_pct"] < 0, "pnl_pct"].sum())
    return wins / losses if losses > 0 else float("nan")


def _null_drawdown_pct(trades_df: pd.DataFrame) -> float:
    if len(trades_df) == 0:
        return 0.0
    return float(trades_df["drawdown_from_high"].isna().mean())


# ── Index builder ──────────────────────────────────────────────────────────

_INDEX_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Phase C Watermark 5% — Per-event charts</title>
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
<h1>Phase C Watermark 5% — Per-event charts</h1>
<div class="meta">{n_events} events with trades &middot; watermark_threshold=5% &middot; click column header to sort</div>
<table id="t">
  <thead>
    <tr>
      <th class="left" data-col="ticker">Ticker<span class="arrow"></span></th>
      <th class="left" data-col="date">Date<span class="arrow"></span></th>
      <th class="left" data-col="session">Session<span class="arrow"></span></th>
      <th data-col="n_trades" data-numeric="1">n_trades<span class="arrow"></span></th>
      <th data-col="n_first" data-numeric="1">n_first<span class="arrow"></span></th>
      <th data-col="n_reentry" data-numeric="1">n_reentry<span class="arrow"></span></th>
      <th data-col="wm_blocked" data-numeric="1">wm_blocked<span class="arrow"></span></th>
      <th data-col="pf" data-numeric="1">event_PF<span class="arrow"></span></th>
      <th data-col="mean_pnl" data-numeric="1">mean_pnl%<span class="arrow"></span></th>
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
  var pfTh=table.querySelector('th[data-col="pf"]');
  if(pfTh)pfTh.click();
}})();
</script>
</body>
</html>
"""


def _row_html(ticker: str, date: str, trades_df: pd.DataFrame,
              n_wm_blocks: int, chart_filename: str) -> str:
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
        f'<td class="left" data-value="{html.escape(session)}">{html.escape(session)}</td>'
        f'<td data-value="{n_t}">{n_t}</td>'
        f'<td data-value="{n_first}">{n_first}</td>'
        f'<td data-value="{n_reentry}">{n_reentry}</td>'
        f'<td data-value="{n_wm_blocks}">{n_wm_blocks}</td>'
        f'<td data-value="{pf_dv}">{pf_str}</td>'
        f'<td data-value="{mean_pnl:.6f}" class="{mean_pnl_cls}">{mean_pnl_str}</td>'
        f'</tr>'
    )


def _write_index(rows_html: list[str], n_events: int, out_path: Path) -> None:
    content = _INDEX_TEMPLATE.format(n_events=n_events, rows="\n".join(rows_html))
    out_path.write_text(content, encoding="utf-8")


# ── Main ───────────────────────────────────────────────────────────────────

def main(tickers: list[str] | None = None, dry_run: bool = False) -> None:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)

    with open(_RESULTS_DIR / "per_event_summary.json") as f:
        all_events = json.load(f)

    per_trade = pd.read_parquet(_RESULTS_DIR / "per_trade.parquet")
    trade_event_keys = set(zip(per_trade["ticker"], per_trade["date"]))

    audit_map = _build_cache_audit_map()

    chartable = [e for e in all_events
                 if (e["ticker"], e["date"]) in trade_event_keys
                 and e.get("n_trades_in_event", 0) > 0]
    if tickers:
        chartable = [e for e in chartable if e["ticker"] in tickers]

    n_total = len(chartable)
    print(f"Phase C watermark chart runner — {n_total} events -> {_OUT_DIR}/")
    print(f"watermark_threshold={_WATERMARK_THRESHOLD:.0%}")
    print()

    errors: list[dict] = []
    index_rows: list[str] = []
    n_written = 0
    null_drawdown_events: list[dict] = []
    total_start = time.time()

    for idx, ev in enumerate(chartable, 1):
        ticker = ev["ticker"]
        date = ev["date"]
        n_wm_blocks = ev.get("n_watermark_blocks", 0)
        ev_start = time.time()

        print(f"[{idx}/{n_total}] {ticker} {date}", end="", flush=True)

        if dry_run:
            print(" [DRY RUN]")
            continue

        out_path = _OUT_DIR / f"{ticker}_{date}.html"
        chart_filename = f"{ticker}_{date}.html"

        try:
            cache_source = audit_map.get((ticker, date), "fallback")
            replay = _load_replay(ticker, date, cache_source)
            if replay is None:
                raise FileNotFoundError(
                    f"No replay cache for {ticker} {date} (source={cache_source})"
                )

            trades_slice = per_trade[
                (per_trade["ticker"] == ticker) & (per_trade["date"] == date)
            ].copy()

            # Flag events where drawdown_from_high is null for >10% of trades
            null_pct = _null_drawdown_pct(trades_slice)
            if null_pct > 0.10:
                null_drawdown_events.append({
                    "ticker": ticker, "date": date,
                    "null_drawdown_pct": round(null_pct * 100, 1),
                    "n_trades": len(trades_slice),
                })

            fig = build_chart(ticker, date, replay, trades_slice, ev,
                              watermark_threshold=_WATERMARK_THRESHOLD)
            fig.write_html(str(out_path), include_plotlyjs="cdn")

            elapsed = time.time() - ev_start
            n_written += 1
            print(f"  OK  {elapsed:.1f}s")

            index_rows.append(_row_html(ticker, date, trades_slice,
                                        n_wm_blocks, chart_filename))

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

    if null_drawdown_events:
        print(f"\nEvents with >10% null drawdown_from_high ({len(null_drawdown_events)}):")
        for e in null_drawdown_events:
            print(f"  {e['ticker']} {e['date']}: {e['null_drawdown_pct']:.1f}% null "
                  f"({e['n_trades']} trades)")
    else:
        print("\nNo events with >10% null drawdown_from_high.")

    if errors:
        print(f"\nErrors ({len(errors)}):")
        for e in errors:
            print(f"  {e['ticker']} {e['date']}: {e['error']}")

    # Escalation check: missing charts
    if not dry_run:
        written_files = {f.stem for f in _OUT_DIR.glob("*.html")
                         if f.name != "index.html"}
        expected = {f"{ev['ticker']}_{ev['date']}" for ev in chartable}
        missing = expected - written_files
        if missing:
            print(f"\nESCALATION: {len(missing)} events with trades have no chart:")
            for m in sorted(missing):
                print(f"  {m}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase C watermark per-event chart runner")
    parser.add_argument("--tickers", nargs="+", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(tickers=args.tickers, dry_run=args.dry_run)
