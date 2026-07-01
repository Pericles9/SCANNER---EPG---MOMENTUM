#!/usr/bin/env python3
"""
Phase EPG-Rapid-Tail-Risk — T6b sortable HTML index.

Organizes every T0-T5 aggregate chart by task, and builds a sortable per-event
table for the T6a union set (CVaR5 tail ∪ bottom decile ∪ pre-market loser mode).
Per-event 4-panel gate charts are reused from the existing p80 event-chart set
(relative link, not duplicated).
"""
from __future__ import annotations

import json
from pathlib import Path

import tail_risk_lib as L

OUT = L.RESULTS / "phase_tail_risk"
EVENT_CHARTS_REL = "../phase_r1_final/event_charts_sym_p80/charts"

# task -> [(file, caption)]
SECTIONS = {
    "T0 · Stratum × Session": [
        ("charts/stratum_session_bar.html", "PF (bars) + WR% (diamonds) per stratum×session cell"),
        ("charts/stratum_session_kde.html", "PnL% KDE by stratum, RTH & pre-market panels (rug = actual trades)"),
    ],
    "T1 · CVaR5 tail set": [
        ("charts/global_pnl_kde_tail.html", "Full-sample PnL% with CVaR5 tail (n=3) + bottom decile (n=6) marked"),
    ],
    "T2 · Pre-market bimodal split": [
        ("charts/premarket_kde_threeway.html", "Pre-market PnL KDE + split boundary + tail + low-stratum markers"),
        ("charts/threeway_venn.html", "3-way overlap: low stratum / CVaR5 tail / pre-market loser mode"),
    ],
    "T3a · Categorical contrasts": [
        ("charts/t3a_categorical_bars/sub_dollar__cvar5tail.html", "Sub-$1 flag — CVaR5 tail vs rest"),
        ("charts/t3a_categorical_bars/sub_dollar__premode.html", "Sub-$1 flag — pre loser vs winner"),
        ("charts/t3a_categorical_bars/sub_dollar__lowstrat.html", "Sub-$1 flag — low vs mid/high"),
        ("charts/t3a_categorical_bars/session_bucket__cvar5tail.html", "Session — CVaR5 tail vs rest"),
        ("charts/t3a_categorical_bars/session_bucket__lowstrat.html", "Session — low vs mid/high"),
        ("charts/t3a_categorical_bars/pm_tod_bucket__cvar5tail.html", "Pre-mkt TOD bucket — tail vs rest"),
        ("charts/t3a_categorical_bars/pm_tod_bucket__premode.html", "Pre-mkt TOD bucket — loser vs winner"),
        ("charts/t3a_categorical_bars/pm_tod_bucket__lowstrat.html", "Pre-mkt TOD bucket — low vs mid/high"),
    ],
    "T3b · Continuous contrasts (KDE overlay)": [
        (f"charts/t3b_continuous_kde/{fk}__{cid}.html", f"{fl} — {cl}")
        for fk, fl in [("gap_pct_at_hit", "Gap % at hit"), ("prev_close", "Prev close $"),
                       ("entry_lag_sec", "Entry lag (s)"), ("n_trades_before_scanner", "Trades before scanner"),
                       ("pre60_count", "Trades 60s pre-entry"), ("pre60_mean_size", "Mean size 60s pre-entry")]
        for cid, cl in [("cvar5tail", "tail vs rest"), ("premode", "pre loser vs winner"),
                        ("lowstrat", "low vs mid/high")]
    ],
    "T3d · Proxy check": [
        ("charts/t3d_gap_mom_scatter.html", "gap_pct_at_hit vs mom_pct scatter + LOWESS (is gap a stratum proxy?)"),
    ],
    "T3e · Cumulative volume at entry (Cooper inverted-U)": [
        ("charts/t3e_volume_kde_by_stratum.html", "Chart 1 — cum $volume KDE by stratum"),
        ("charts/t3e_volume_bins_bar.html", "Chart 2 — volume quintiles → PF & mean PnL (primary test)"),
        ("charts/t3e_volume_mom_scatter.html", "Chart 3 — cum $volume vs mom_pct"),
        ("charts/t3e_volume_pnl_scatter.html", "Chart 4 — cum $volume vs PnL%, faceted by stratum (headline)"),
    ],
    "T4 · Post-entry features": [
        ("charts/t4a_rth_crossing_bar.html", "T4a — % of trades whose hold crosses 09:30 ET"),
        ("charts/t4a2_volume_trajectory.html", "T4a2 — post-entry cumulative $volume, median+IQR, first 300s"),
        ("charts/t4b_hold_duration_kde__cvar5tail.html", "T4b — hold duration, tail vs rest"),
        ("charts/t4b_hold_duration_kde__premode.html", "T4b — hold duration, pre loser vs winner"),
        ("charts/t4b_hold_duration_kde__lowstrat.html", "T4b — hold duration, low vs mid/high"),
    ],
    "T5 · Stability across p sweep": [
        ("charts/t5_stability_line.html", "CVaR5 tail-set size + %overlap-with-low across gate p"),
    ],
}


def build_union(df):
    boundary, assign, _ = L.premarket_mode_split(df)
    loser = set(zip(assign[assign.pm_mode == "loser"].ticker, assign[assign.pm_mode == "loser"].date))
    rows = []
    for _, r in df.iterrows():
        key = (r.ticker, r.date)
        in_tail = bool(r.in_cvar5_tail)
        in_dec = bool(r.in_bottom_decile)
        in_loser = key in loser
        if not (in_tail or in_dec or in_loser):
            continue
        sets = []
        if in_tail:
            sets.append("tail")
        if in_dec:
            sets.append("decile")
        if in_loser:
            sets.append("pre-loser")
        rows.append(dict(ticker=r.ticker, date=r.date, stratum=r.stratum,
                         session=r.session_bucket, pnl=round(r.pnl_pct, 2),
                         sets=" ".join(sets)))
    rows.sort(key=lambda x: x["pnl"])
    return rows


def render(df):
    union = build_union(df)
    parts = []
    parts.append("""<!doctype html><html><head><meta charset="utf-8">
<title>Phase EPG-Rapid-Tail-Risk — Chart Index</title>
<style>
body{background:#111417;color:#e0e0e0;font-family:Segoe UI,Arial,sans-serif;margin:0 auto;max-width:1150px;padding:24px}
h1{color:#fff;border-bottom:2px solid #2196F3;padding-bottom:8px}
h2{color:#90CAF9;margin-top:28px;font-size:17px}
a{color:#64B5F6;text-decoration:none}a:hover{text-decoration:underline}
ul{line-height:1.9;list-style:none;padding-left:0}
li{padding:2px 0}.cap{color:#9e9e9e;font-size:13px}
table{border-collapse:collapse;width:100%;margin-top:8px;font-size:14px}
th,td{border:1px solid #2a2f36;padding:6px 10px;text-align:left}
th{background:#1b1f24;cursor:pointer;user-select:none}th:hover{background:#252b32}
th.sorted-asc::after{content:" ▲";color:#2196F3}th.sorted-desc::after{content:" ▼";color:#2196F3}
tr:nth-child(even){background:#161a1e}
.neg{color:#EF5350}.pos{color:#66BB6A}
.tag{display:inline-block;background:#263238;border-radius:3px;padding:1px 6px;margin-right:3px;font-size:12px}
.note{color:#9e9e9e;font-size:13px;background:#181c20;padding:10px 14px;border-left:3px solid #FF9800;margin:12px 0}
</style></head><body>""")
    parts.append("<h1>Phase EPG-Rapid-Tail-Risk — Chart Index</h1>")
    parts.append("<p class='cap'>Reference: sym_p80 · val_r4_stratified · T_gate=500s. "
                 "Chart-first diagnostic (T0–T6). No remediation proposed.</p>")

    for section, items in SECTIONS.items():
        parts.append(f"<h2>{section}</h2><ul>")
        for f, cap in items:
            parts.append(f"<li><a href='{f}'>{f.split('/')[-1]}</a> "
                         f"<span class='cap'>— {cap}</span></li>")
        parts.append("</ul>")

    # T6a per-event sortable table
    parts.append("<h2>T6a · Per-event 4-panel gate charts — union set "
                 f"(n={len(union)})</h2>")
    parts.append("<div class='note'>Per-event charts are reused from the existing p80 "
                 "event-chart set (linked, not duplicated). Click a header to sort; "
                 "Stratum / Session / PnL / Sets all sortable.</div>")
    parts.append("<table id='evtbl'><thead><tr>"
                 "<th onclick='sortT(0)'>Ticker</th>"
                 "<th onclick='sortT(1)'>Date</th>"
                 "<th onclick='sortT(2)'>Stratum</th>"
                 "<th onclick='sortT(3)'>Session</th>"
                 "<th onclick='sortT(4)'>PnL%</th>"
                 "<th onclick='sortT(5)'>Sets</th>"
                 "<th>Chart</th></tr></thead><tbody>")
    for r in union:
        cls = "neg" if r["pnl"] < 0 else "pos"
        tags = " ".join(f"<span class='tag'>{s}</span>" for s in r["sets"].split())
        link = f"{EVENT_CHARTS_REL}/{r['ticker']}_{r['date']}.html"
        parts.append(
            f"<tr><td>{r['ticker']}</td><td>{r['date']}</td><td>{r['stratum']}</td>"
            f"<td>{r['session']}</td>"
            f"<td data-val='{r['pnl']:.4f}' class='{cls}'>{r['pnl']:+.2f}</td>"
            f"<td data-val='{r['sets']}'>{tags}</td>"
            f"<td><a href='{link}'>open</a></td></tr>")
    parts.append("</tbody></table>")

    parts.append("""<script>
let _c=-1,_a=true;
function sortT(col){
 const t=document.getElementById('evtbl');
 const rows=Array.from(t.querySelectorAll('tbody tr'));
 _a=(_c===col)?!_a:true;_c=col;
 rows.sort((x,y)=>{
   const av=x.cells[col].dataset.val??x.cells[col].textContent.trim();
   const bv=y.cells[col].dataset.val??y.cells[col].textContent.trim();
   const an=parseFloat(av),bn=parseFloat(bv);
   if(!isNaN(an)&&!isNaN(bn))return _a?an-bn:bn-an;
   return _a?av.localeCompare(bv):bv.localeCompare(av);
 });
 const tb=t.querySelector('tbody');rows.forEach(r=>tb.appendChild(r));
 t.querySelectorAll('thead th').forEach((th,i)=>{th.classList.remove('sorted-asc','sorted-desc');
   if(i===col)th.classList.add(_a?'sorted-asc':'sorted-desc');});
}
</script></body></html>""")

    (OUT / "index.html").write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {OUT / 'index.html'}  (union n={len(union)})")


if __name__ == "__main__":
    df = L.load_joined("p80")
    render(df)
