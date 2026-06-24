"""Parse and tabulate R1 p_open x p_close sweep results."""
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

p_open_vals  = [0.45, 0.50, 0.55, 0.60, 0.65, 0.70]
p_close_vals = [0.55, 0.60, 0.65, 0.70, 0.75]

rows = []
for po in p_open_vals:
    for pc in p_close_vals:
        tag = f"po{int(po*100)}_pc{int(pc*100)}"
        p = BASE / "results" / "phase_r1_mdr150" / tag / "run_summary.json"
        if not p.exists():
            rows.append({"po": po, "pc": pc, "tag": tag, "n": None, "pf": None,
                         "wr": None, "mp": None, "cv": None, "warn": "missing"})
            continue
        with open(p) as f:
            s = json.load(f)
        rows.append({
            "po": po, "pc": pc, "tag": tag,
            "n":  s.get("n_trades"),
            "pf": s.get("profit_factor"),
            "wr": s.get("win_rate"),
            "mp": s.get("mean_pnl_pct"),
            "cv": s.get("cvar5_pct"),
            "warn": s.get("warning", ""),
        })

# Diagnose zero-trade runs
zero = [r for r in rows if r["n"] is not None and r["n"] == 0]
if zero:
    print(f"{len(zero)} runs with 0 trades:")
    for r in zero:
        print(f"  {r['tag']}  warning={r['warn']}")
    print()

print("PF grid  (rows=p_open, cols=p_close):")
header = "        " + "".join(f"  pc={pc:.2f}" for pc in p_close_vals)
print(header)
for po in p_open_vals:
    line = f"po={po:.2f} "
    for pc in p_close_vals:
        r = next(x for x in rows if x["po"] == po and x["pc"] == pc)
        if r["pf"] is None:
            val = "ERR"
        elif r["n"] == 0:
            val = "0tr"
        else:
            val = f"{r['pf']:.3f}"
        line += f"  {val:7s}"
    print(line)

print()
print("n_trades grid:")
print(header)
for po in p_open_vals:
    line = f"po={po:.2f} "
    for pc in p_close_vals:
        r = next(x for x in rows if x["po"] == po and x["pc"] == pc)
        val = str(r["n"]) if r["n"] is not None else "ERR"
        line += f"  {val:7s}"
    print(line)

print()
valid = [r for r in rows if r["pf"] is not None and r["n"] and r["n"] > 0]
valid.sort(key=lambda r: r["pf"], reverse=True)
print("All results by PF (excluding 0-trade runs):")
print(f"  {'po':6s} {'pc':6s} {'n':4s}  {'PF':8s}  {'win%':6s}  {'mean':7s}  {'CVaR5':7s}")
for r in valid:
    marker = " <-- baseline" if r["po"] == 0.65 and r["pc"] == 0.65 else ""
    print(f"  {r['po']:.2f}   {r['pc']:.2f}  {r['n']:3d}  {r['pf']:8.3f}  {r['wr']:5.1f}%  {r['mp']:6.2f}%  {r['cv']:6.2f}%{marker}")
