#!/usr/bin/env python3
"""
Structural Mitigation — T6 sortable index.

Links every T2-T5 + T7 chart, plus a sortable per-event table for the union of affected
trades (T3 stop-cut ∪ T5 guard-excluded). T5 guard was null → excluded set empty, so the
union is the 15 stop-cut trades. Per-event 4-panel charts reuse the existing p80 set (linked).
"""
from __future__ import annotations

import json
import tail_risk_lib as L

SM = L.RESULTS / "phase_tail_risk" / "structural_mitigation"
EVENT_REL = "../../phase_r1_final/event_charts_sym_p80/charts"

SECTIONS = {
    "T2 · MAE/MFE gap diagnostic": [
        ("charts/mae_gap_kde.html", "winner drawdown (MAE) vs loser final PnL — is there a gap?"),
        ("charts/mae_pnl_scatter.html", "MAE vs final PnL%, colored by win/loss"),
        ("mae_mfe_verdict.md", "T2a percentiles + T2b verdict (markdown)"),
    ],
    "T3 · Test A — MAE-gap disaster stop (NEGATIVE)": [
        ("charts/stop_pnl_kde.html", "PnL distribution: baseline vs −9.59% stop"),
        ("charts/stop_comparison_bar.html", "PF/WR/mean/CVaR5: baseline vs stop vs T_gate=300"),
        ("stop_test_summary.md", "T3a counts + negative verdict (markdown)"),
    ],
    "T4 · Test B — staged entry (NEGATIVE)": [
        ("charts/staged_comparison_bar.html", "PF/WR/mean/CVaR5: baseline vs staged vs T_gate=300"),
        ("charts/staged_confirm_rate_bar.html", "2nd-tranche confirm rate: winners vs losers"),
        ("staged_entry_summary.md", "T4a EV-cost finding (markdown)"),
    ],
    "T5 · Test C — spread-width guard (NULL)": [
        ("charts/spread_gap_kde.html", "entry spread%: winners vs losers (no difference)"),
        ("spread_guard_summary.md", "T5a null + T5b/c skipped (markdown)"),
    ],
    "T7 · Consolidated": [
        ("charts/final_comparison_bar.html", "all configs side by side"),
        ("summary.md", "consolidated verdict (markdown)"),
    ],
}


def build_union():
    tr = json.load(open(SM / "raw_trajectories.json"))["trades"]
    stop = json.load(open(SM / "t2_verdict.json"))["stop_level_pct"]
    rows = []
    for r in tr:
        cut_pnl = None
        for _, pnl, _ in r["mae_envelope"]:
            if pnl <= stop:
                cut_pnl = pnl
                break
        if cut_pnl is None:
            continue  # not affected by T3 stop; T5 guard excluded none
        rows.append(dict(ticker=r["ticker"], date=r["date"], stratum=r["stratum"],
                         session=r["session_bucket"], baseline=round(r["baseline_pnl_pct"], 2),
                         stopped=round(float(cut_pnl), 2), is_winner=r["is_winner"],
                         effect="FALSE CUT (winner)" if r["is_winner"] else
                                ("rescued" if r["baseline_pnl_pct"] < cut_pnl else "made worse")))
    rows.sort(key=lambda x: x["baseline"])
    return rows, stop


def main():
    union, stop = build_union()
    p = ["""<!doctype html><html><head><meta charset="utf-8">
<title>Structural Mitigation — Chart Index</title><style>
body{background:#111417;color:#e0e0e0;font-family:Segoe UI,Arial,sans-serif;margin:0 auto;max-width:1100px;padding:24px}
h1{color:#fff;border-bottom:2px solid #26a69a;padding-bottom:8px}h2{color:#80CBC4;margin-top:26px;font-size:16px}
a{color:#64B5F6;text-decoration:none}a:hover{text-decoration:underline}ul{line-height:1.9;list-style:none;padding-left:0}
.cap{color:#9e9e9e;font-size:13px}table{border-collapse:collapse;width:100%;margin-top:8px;font-size:14px}
th,td{border:1px solid #2a2f36;padding:6px 10px;text-align:left}th{background:#1b1f24;cursor:pointer;user-select:none}
th:hover{background:#252b32}th.sa::after{content:" ▲";color:#26a69a}th.sd::after{content:" ▼";color:#26a69a}
tr:nth-child(even){background:#161a1e}.neg{color:#EF5350}.pos{color:#66BB6A}
.note{color:#9e9e9e;font-size:13px;background:#181c20;padding:10px 14px;border-left:3px solid #FF9800;margin:12px 0}
</style></head><body>"""]
    p.append("<h1>Tail-Risk · Structural Mitigation — Chart Index</h1>")
    p.append("<p class='cap'>Baseline: R1-Final sym_p80 (no gate) · val_r4_stratified. "
             "Three prediction-free candidates: MAE-gap stop (T3), staged entry (T4), spread guard (T5). "
             "All negative/null — see T7.</p>")
    for sec, items in SECTIONS.items():
        p.append(f"<h2>{sec}</h2><ul>")
        for f, cap in items:
            p.append(f"<li><a href='{f}'>{f.split('/')[-1]}</a> <span class='cap'>— {cap}</span></li>")
        p.append("</ul>")

    p.append(f"<h2>T6 · Per-event charts — affected trades (union, n={len(union)})</h2>")
    p.append("<div class='note'>Union = T3 stop-cut trades (T5 guard excluded none, being null). "
             "Per-event 4-panel gate charts reuse the existing p80 set (linked). Click a header to sort.</div>")
    p.append("<table id='t'><thead><tr>"
             "<th onclick='S(0)'>Ticker</th><th onclick='S(1)'>Date</th><th onclick='S(2)'>Stratum</th>"
             "<th onclick='S(3)'>Session</th><th onclick='S(4)'>Baseline%</th><th onclick='S(5)'>Stopped%</th>"
             "<th onclick='S(6)'>Stop effect</th><th>Chart</th></tr></thead><tbody>")
    for r in union:
        bcls = "neg" if r["baseline"] < 0 else "pos"
        link = f"{EVENT_REL}/{r['ticker']}_{r['date']}.html"
        p.append(f"<tr><td>{r['ticker']}</td><td>{r['date']}</td><td>{r['stratum']}</td>"
                 f"<td>{r['session']}</td>"
                 f"<td data-val='{r['baseline']:.4f}' class='{bcls}'>{r['baseline']:+.2f}</td>"
                 f"<td data-val='{r['stopped']:.4f}' class='neg'>{r['stopped']:+.2f}</td>"
                 f"<td>{r['effect']}</td><td><a href='{link}'>open</a></td></tr>")
    p.append("</tbody></table>")
    p.append("""<script>
let c=-1,a=true;function S(col){const t=document.getElementById('t');
const rows=Array.from(t.querySelectorAll('tbody tr'));a=(c===col)?!a:true;c=col;
rows.sort((x,y)=>{const av=x.cells[col].dataset.val??x.cells[col].textContent.trim();
const bv=y.cells[col].dataset.val??y.cells[col].textContent.trim();const an=parseFloat(av),bn=parseFloat(bv);
if(!isNaN(an)&&!isNaN(bn))return a?an-bn:bn-an;return a?av.localeCompare(bv):bv.localeCompare(av);});
const tb=t.querySelector('tbody');rows.forEach(r=>tb.appendChild(r));
t.querySelectorAll('thead th').forEach((th,i)=>{th.classList.remove('sa','sd');if(i===col)th.classList.add(a?'sa':'sd');});}
</script></body></html>""")
    (SM / "index.html").write_text("\n".join(p), encoding="utf-8")
    print(f"  wrote index.html (union n={len(union)})")


if __name__ == "__main__":
    main()
