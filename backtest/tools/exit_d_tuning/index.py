"""Sortable HTML index page for an EXIT_D parameter sweep.

Plain HTML + ~30 lines of vanilla JS for column sorting. No framework.
"""
from __future__ import annotations

import html
from pathlib import Path
from typing import List

from tools.exit_d_tuning.chart import ChartSummary


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
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
  td.delta-pos {{ color: #0a8; font-weight: 600; }}
  td.delta-neg {{ color: #c33; font-weight: 600; }}
  a {{ color: #0066cc; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .note {{ color: #555; font-size: 12px; margin-top: 18px; max-width: 720px; }}
</style>
</head>
<body>
<h1>{title}</h1>
<div class="meta">{n_rows} parameter combinations &middot; click any column to re-sort</div>

<table id="sweep">
  <thead>
    <tr>
      <th data-col="theta" data-numeric="1">theta<span class="arrow"></span></th>
      <th data-col="tau" data-numeric="1">tau_min_sec<span class="arrow"></span></th>
      <th data-col="fired" data-numeric="1">n_exit_d_fired<span class="arrow"></span></th>
      <th data-col="ntrades" data-numeric="1">n_trades<span class="arrow"></span></th>
      <th data-col="delta" data-numeric="1">sum_delta_pnl_pct<span class="arrow"></span></th>
      <th data-col="improved" data-numeric="1">n_improved<span class="arrow"></span></th>
      <th data-col="worsened" data-numeric="1">n_worsened<span class="arrow"></span></th>
      <th data-col="link" class="left">Open</th>
    </tr>
  </thead>
  <tbody>
{rows}
  </tbody>
</table>

<p class="note">
  &Delta;-pnl is the sum of <code>(exit_d_pnl_pct &minus; original_pnl_pct)</code> across
  all trades in the event. Trades where EXIT_D didn't fire contribute 0 to the delta
  (the original exit stands).
</p>

<script>
(function() {{
  var table = document.getElementById('sweep');
  var tbody = table.querySelector('tbody');
  var headers = table.querySelectorAll('th');
  var sortState = {{}};

  function sortBy(colIdx, numeric, asc) {{
    var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
    rows.sort(function(a, b) {{
      var av = a.children[colIdx].getAttribute('data-value');
      var bv = b.children[colIdx].getAttribute('data-value');
      if (numeric) {{ av = parseFloat(av); bv = parseFloat(bv); }}
      if (av < bv) return asc ? -1 : 1;
      if (av > bv) return asc ? 1 : -1;
      return 0;
    }});
    rows.forEach(function(r) {{ tbody.appendChild(r); }});
  }}

  headers.forEach(function(th, idx) {{
    var col = th.getAttribute('data-col');
    if (!col) return;
    th.addEventListener('click', function() {{
      var numeric = th.getAttribute('data-numeric') === '1';
      var asc = sortState[col] === 'asc' ? false : true;
      sortState = {{}}; sortState[col] = asc ? 'asc' : 'desc';
      headers.forEach(function(h) {{
        var arrow = h.querySelector('.arrow');
        if (arrow) arrow.textContent = '';
      }});
      var arrow = th.querySelector('.arrow');
      if (arrow) arrow.textContent = asc ? '▲' : '▼';
      sortBy(idx, numeric, asc);
    }});
  }});

  // Default: sort by sum_delta_pnl_pct descending
  var deltaTh = table.querySelector('th[data-col="delta"]');
  if (deltaTh) deltaTh.click();
}})();
</script>
</body>
</html>
"""


def _row_html(s: ChartSummary) -> str:
    delta_class = ("delta-pos" if s.sum_delta_pnl_pct > 0
                    else ("delta-neg" if s.sum_delta_pnl_pct < 0 else ""))
    return (
        "    <tr>"
        f"<td data-value=\"{s.theta:.4f}\">{s.theta:.2f}</td>"
        f"<td data-value=\"{s.tau_min_sec:.4f}\">{s.tau_min_sec:.1f}</td>"
        f"<td data-value=\"{s.n_exit_d_fired}\">{s.n_exit_d_fired}</td>"
        f"<td data-value=\"{s.n_trades}\">{s.n_trades}</td>"
        f"<td data-value=\"{s.sum_delta_pnl_pct:.6f}\" "
        f"class=\"{delta_class}\">{s.sum_delta_pnl_pct:+.3f}%</td>"
        f"<td data-value=\"{s.n_improved_trades}\">{s.n_improved_trades}</td>"
        f"<td data-value=\"{s.n_worsened_trades}\">{s.n_worsened_trades}</td>"
        "<td class=\"left\" data-value=\"\">"
        f"<a href=\"{html.escape(s.chart_filename)}\">{html.escape(s.chart_filename)}</a>"
        "</td>"
        "</tr>"
    )


def write_index(
    summaries: List[ChartSummary],
    ticker: str,
    date: str,
    output_path: Path,
) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    title = (f"{html.escape(ticker)} {html.escape(date)} — "
             f"EXIT_D parameter sweep")
    rows_html = "\n".join(_row_html(s) for s in summaries)

    out = _HTML_TEMPLATE.format(
        title=title,
        n_rows=len(summaries),
        rows=rows_html,
    )
    output_path.write_text(out, encoding="utf-8")
