"""Phase E per-event chart runner -- symmetric spread-multiple LULD exit.

For each event with trades in results/phase_e/sweep_N{n}/:
  1. Load EventReplay from cache (phase_b -> phase_u fallback)
  2. Load QuoteData for LULD band visualization
  3. Build 4-panel chart (price + LULD bands + window high, I(t), drawdown, EPG state)
  4. Write standalone HTML to results/phase_e/event_charts_N{n}/

Writes sortable index.html at the end.

Usage:
    python -m tools.phase_e.run_charts --n 2
    python -m tools.phase_e.run_charts --n 1 --tickers SVRE XBP
    python -m tools.phase_e.run_charts --n 2 --dry-run
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
from tools.phase_e.chart import build_chart
from data.loaders.quotes import load_quotes
from data.loaders.trades import list_events

_ROOT = Path(__file__).resolve().parents[2]
_PHASE_B_CACHE = _ROOT / "results" / "phase_b" / "replay_caches"
_PHASE_U_CACHE = Path(r"D:\Trading Research\hawkes-ofi-impact\results\phase_u\replay_caches")
_PHASE_B_AUDIT = (_ROOT / "results" / "phase_b" / "100_val_seed42"
                  / "event_charts" / "cache_audit.json")
_PHASE_E_BASE = _ROOT / "results" / "phase_e"

_THRESHOLD = 0.02


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


def _resolve_mom_pct(ticker: str, date: str) -> float | None:
    for ev in list_events(min_mom=0.0, require_date=True):
        if ev["ticker"] == ticker and ev["date"] == date:
            return ev["mom_pct"]
    return None


def _load_quotes_safe(ticker: str, date: str):
    mom_pct = _resolve_mom_pct(ticker, date)
    if mom_pct is None:
        return None
    try:
        return load_quotes(ticker, date, mom_pct)
    except Exception:
        return None


def _pf(df: pd.DataFrame) -> float:
    wins = df.loc[df["pnl_pct"] > 0, "pnl_pct"].sum()
    losses = abs(df.loc[df["pnl_pct"] < 0, "pnl_pct"].sum())
    return wins / losses if losses > 0 else float("nan")


_INDEX_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Phase E LULD N={n_spread_multiple} -- Per-event charts</title>
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
<h1>Phase E LULD N={n_spread_multiple} -- Per-event charts</h1>
<div class="meta">{n_events} events with trades &middot; N={n_spread_multiple} spread multiple &middot; wm=2% &middot; click column header to sort</div>
<table id="t">
  <thead>
    <tr>
      <th class="left" data-col="ticker">Ticker<span class="arrow"></span></th>
      <th class="left" data-col="date">Date<span class="arrow"></span></th>
      <th class="left" data-col="session">Session<span class="arrow"></span></th>
      <th data-col="n_trades" data-numeric="1">n_trades<span class="arrow"></span></th>
      <th data-col="n_first" data-numeric="1">n_first<span class="arrow"></span></th>
      <th data-col="n_reentry" data-numeric="1">n_reentry<span class="arrow"></span></th>
      <th data-col="n_luld_lower" data-numeric="1">luld_lo<span class="arrow"></span></th>
      <th data-col="n_luld_upper" data-numeric="1">luld_hi<span class="arrow"></span></th>
      <th data-col="n_first_blocked" data-numeric="1">first_blocked<span class="arrow"></span></th>
      <th data-col="n_re_blocked" data-numeric="1">re_blocked<span class="arrow"></span></th>
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
      var arrow=th.querySelector('.arrow');if(arrow)arrow.textContent=asc?'up':'down';
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


def _row_html(
    ticker: str,
    date: str,
    trades_df: pd.DataFrame,
    n_first_blocked: int,
    n_re_blocked: int,
    chart_filename: str,
) -> str:
    n_t = len(trades_df)
    n_first = int((trades_df["entry_type"] == "first").sum())
    n_reentry = int((trades_df["entry_type"] == "reentry").sum())
    exit_reasons = trades_df["exit_reason"].values if n_t else np.array([])
    n_luld_lo = int(np.sum(exit_reasons == "luld_lower"))
    n_luld_hi = int(np.sum(exit_reasons == "luld_upper"))
    pf_val = _pf(trades_df)
    pf_str = f"{pf_val:.4f}" if not np.isnan(pf_val) else "N/A"
    pf_dv = f"{pf_val:.6f}" if not np.isnan(pf_val) else "0"
    mean_pnl = trades_df["pnl_pct"].mean() if n_t else 0.0
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
        f'<td data-value="{n_luld_lo}">{n_luld_lo}</td>'
        f'<td data-value="{n_luld_hi}">{n_luld_hi}</td>'
        f'<td data-value="{n_first_blocked}">{n_first_blocked}</td>'
        f'<td data-value="{n_re_blocked}">{n_re_blocked}</td>'
        f'<td data-value="{pf_dv}">{pf_str}</td>'
        f'<td data-value="{mean_pnl:.6f}" class="{mean_pnl_cls}">{mean_pnl_str}</td>'
        f'</tr>'
    )


def _write_index(
    rows_html: list[str],
    n_events: int,
    n_spread_multiple: int,
    out_path: Path,
) -> None:
    out_path.write_text(
        _INDEX_TEMPLATE.format(
            n_events=n_events,
            n_spread_multiple=n_spread_multiple,
            rows="\n".join(rows_html),
        ),
        encoding="utf-8",
    )


def main(n_spread_multiple: int, tickers: list[str] | None = None, dry_run: bool = False) -> None:
    results_dir = _PHASE_E_BASE / f"sweep_N{n_spread_multiple}"
    out_dir = _PHASE_E_BASE / f"event_charts_N{n_spread_multiple}"
    errors_path = out_dir / "chart_errors.json"

    if not results_dir.exists():
        print(f"ERROR: {results_dir} does not exist -- run sweep first")
        sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)

    per_trade = pd.read_parquet(results_dir / "per_trade.parquet")
    blocked_all = pd.read_parquet(results_dir / "blocked_edges.parquet")

    trade_event_keys = set(zip(per_trade["ticker"], per_trade["date"]))
    audit_map = _build_cache_audit_map()

    events = sorted(trade_event_keys)
    if tickers:
        events = [(t, d) for t, d in events if t in tickers]

    n_total = len(events)
    print(f"Phase E chart runner -- N={n_spread_multiple} -- {n_total} events -> {out_dir}/")
    print(f"threshold={_THRESHOLD:.0%}")
    print()

    errors: list[dict] = []
    index_rows: list[str] = []
    n_written = 0
    total_start = time.time()

    for idx, (ticker, date) in enumerate(events, 1):
        ev_start = time.time()
        print(f"[{idx}/{n_total}] {ticker} {date}", end="", flush=True)

        if dry_run:
            print(" [DRY RUN]")
            continue

        out_path = out_dir / f"{ticker}_{date}.html"
        chart_filename = f"{ticker}_{date}.html"

        try:
            cache_source = audit_map.get((ticker, date), "fallback")
            replay = _load_replay(ticker, date, cache_source)
            if replay is None:
                raise FileNotFoundError(
                    f"No replay cache for {ticker} {date} (source={cache_source})"
                )

            qd = _load_quotes_safe(ticker, date)
            if qd is None:
                print(" [no quotes]", end="", flush=True)

            trades_slice = per_trade[
                (per_trade["ticker"] == ticker) & (per_trade["date"] == date)
            ].copy()
            blocked_slice = blocked_all[
                (blocked_all["ticker"] == ticker) & (blocked_all["date"] == date)
            ].copy()

            n_first_blocked = int((blocked_slice["entry_type"] == "first").sum())
            n_re_blocked = int((blocked_slice["entry_type"] == "reentry").sum())

            fig = build_chart(
                ticker, date, replay, trades_slice, blocked_slice,
                threshold=_THRESHOLD,
                n_spread_multiple=float(n_spread_multiple),
                qd=qd,
            )
            fig.write_html(str(out_path), include_plotlyjs="cdn")

            elapsed = time.time() - ev_start
            n_written += 1
            print(f"  OK  {elapsed:.1f}s")

            index_rows.append(_row_html(
                ticker, date, trades_slice,
                n_first_blocked, n_re_blocked,
                chart_filename,
            ))

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

    with open(errors_path, "w") as f:
        json.dump(errors, f, indent=2)

    if not dry_run:
        _write_index(index_rows, n_written, n_spread_multiple, out_dir / "index.html")
        print(f"\nindex.html written ({n_written} events)")

    print(f"\nDone. {n_written}/{n_total} charts | {len(errors)} errors | "
          f"{total_elapsed:.1f}s total")

    if errors:
        print(f"\nErrors ({len(errors)}):")
        for e in errors:
            print(f"  {e['ticker']} {e['date']}: {e['error']}")

    if not dry_run:
        written_files = {f.stem for f in out_dir.glob("*.html")
                         if f.name != "index.html"}
        expected = {f"{t}_{d}" for t, d in events}
        missing = expected - written_files
        if missing:
            print(f"\nESCALATION: {len(missing)} events with trades have no chart:")
            for m in sorted(missing):
                print(f"  {m}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase E per-event chart runner")
    parser.add_argument("--n", type=int, required=True,
                        help="Spread multiple (1, 2, or 3) -- must match sweep_N{n} dir")
    parser.add_argument("--tickers", nargs="+", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(n_spread_multiple=args.n, tickers=args.tickers, dry_run=args.dry_run)
