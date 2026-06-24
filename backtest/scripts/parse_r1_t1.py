"""Parse and report R1 T1 symmetric sweep results."""
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent  # scanner-epg-momentum/
RESULTS_BASE = BASE / "backtest" / "results" / "phase_r1_mdr150"

THRESHOLDS = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75]

# R0 baseline for escalation checks
R0_PF = 2.97
R0_N  = 96

rows = []
for p in THRESHOLDS:
    tag = f"sym_p{int(p*100)}"
    path = RESULTS_BASE / tag / "run_summary.json"
    if not path.exists():
        rows.append({"p": p, "tag": tag, "status": "missing"})
        continue
    with open(path) as f:
        s = json.load(f)
    rows.append({
        "p":    p,
        "tag":  tag,
        "n":    s.get("n_trades"),
        "pf":   s.get("profit_factor"),
        "wr":   s.get("win_rate"),
        "mp":   s.get("mean_pnl_pct"),
        "cv":   s.get("cvar5_pct"),
        "mh":   s.get("mean_hold_sec"),
        "mel":  s.get("mean_entry_lag_from_scanner_sec"),
        "warn": s.get("warning", ""),
        "status": "ok",
    })

print("=" * 70)
print("R1 T1 — Symmetric EPG Gate Threshold Sweep")
print(f"Baseline R0: PF={R0_PF}  n={R0_N}")
print("=" * 70)
print()

# Summary table
hdr = f"{'p':6s} {'n':4s}  {'PF':8s}  {'win%':6s}  {'mean%':7s}  {'CVaR5':7s}  {'hold_s':7s}  {'lag_s':6s}"
print(hdr)
print("-" * len(hdr))
for r in rows:
    if r["status"] == "missing":
        print(f"  {r['p']:.2f}  -- MISSING --")
        continue
    baseline = " <-- baseline" if r["p"] == 0.65 else ""
    if r["n"] is None or r["n"] == 0:
        print(f"  {r['p']:.2f}  {0:4d}  {'0 trades':8s}{baseline}")
        continue
    pf_str  = f"{r['pf']:.3f}" if r["pf"] is not None else "n/a"
    wr_str  = f"{r['wr']:.1f}%" if r["wr"] is not None else "n/a"
    mp_str  = f"{r['mp']:.2f}%" if r["mp"] is not None else "n/a"
    cv_str  = f"{r['cv']:.2f}%" if r["cv"] is not None else "n/a"
    mh_str  = f"{r['mh']:.0f}" if r["mh"] is not None else "n/a"
    mel_str = f"{r['mel']:.0f}" if r["mel"] is not None else "n/a"
    print(f"  {r['p']:.2f}  {r['n']:4d}  {pf_str:8s}  {wr_str:6s}  {mp_str:7s}  {cv_str:7s}  {mh_str:7s}  {mel_str:6s}{baseline}")

print()

# Escalation checks
print("Escalation checks:")
valid = [r for r in rows if r["status"] == "ok" and r["n"] and r["n"] > 0 and r["pf"] is not None]
if not valid:
    print("  No valid results — all configs missing or 0 trades.")
else:
    best_cvar = max(r["cv"] for r in valid if r["cv"] is not None)
    all_pf_below = all(r["pf"] < R0_PF for r in valid)
    if best_cvar < -15.0:
        print(f"  HARD STOP: best CVaR5={best_cvar:.2f}% < -15% threshold.")
    else:
        print(f"  CVaR5 OK: best={best_cvar:.2f}%")
    if all_pf_below:
        print(f"  HARD STOP: all configs PF < R0 baseline ({R0_PF}).")
    else:
        best_pf = max(r["pf"] for r in valid)
        print(f"  PF OK: best={best_pf:.3f} >= R0 baseline ({R0_PF})")

print()
print("Done. Present table to Cooper for p selection before running T2.")
