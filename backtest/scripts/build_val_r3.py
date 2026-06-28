"""
Phase R3 T1 — build the stratified val sample (val_r3_stratified.json).

Source:   DATA_ROOT/filtered/scanner_hit_catalog.json (val split, confirmed hits).
Exclude:  the 100 MDR>=200 diagnostic events (val_mdr150_diagnostic.json).
Stratify: gap_pct_at_hit = (scanner_hit_price/prev_close - 1)*100  (no look-ahead).
  low  [30,100)  target 50
  mid  [100,200) target 30  (only 16 available -> take all; total N reduced)
  high [200, inf) target 20
Sampling: random within stratum, seed=42, no duplicate (ticker,date).
T1a: every sampled event must have loadable tick data (n_trades>=30); else draw the
     next event in the shuffled stratum order (record the replacement).
"""
from __future__ import annotations
import json
import random
import sys
from pathlib import Path

BACKTEST = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKTEST))

from data.schemas.mom_db import DATA_ROOT
from data.loaders.trades import list_events, load_trades

VAL_START, VAL_END = "2023-11-17", "2024-07-23"
SEED = 42
TARGETS = {"low": 50, "mid": 30, "high": 20}
OUT = BACKTEST / "data" / "val_r3_stratified.json"
MDR200 = Path(r"D:\Trading Research\data\val_mdr150_diagnostic.json")


def stratum_of(gap):
    if 30 <= gap < 100:
        return "low"
    if 100 <= gap < 200:
        return "mid"
    if gap >= 200:
        return "high"
    return None


def main():
    cat = json.load(open(DATA_ROOT / "filtered" / "scanner_hit_catalog.json"))
    excl = {(e["ticker"], e["date"]) for e in json.load(open(MDR200))["events"]}
    mom = {(e["ticker"], e["date"]): e["mom_pct"]
           for e in list_events(min_mom=50.0, require_date=True)
           if VAL_START <= e["date"] < VAL_END}

    pools = {"low": [], "mid": [], "high": []}
    for r in cat.values():
        if r.get("scanner_hit_ts_ns") is None:
            continue
        t, d = r["ticker"], r["date"]
        if not (VAL_START <= d < VAL_END) or (t, d) in excl or (t, d) not in mom:
            continue
        pc, shp = r.get("prev_close"), r.get("scanner_hit_price")
        if not pc or not shp:
            continue
        gap = (shp / pc - 1) * 100.0
        st = stratum_of(gap)
        if st is None:
            continue
        pools[st].append({
            "ticker": t, "date": d, "mom_pct": mom[(t, d)],
            "scanner_hit_ts_ns": r["scanner_hit_ts_ns"],
            "scanner_hit_tod_sec": r["scanner_hit_tod_sec"],
            "scanner_hit_price": r["scanner_hit_price"],
            "scanner_hit_idx": r["scanner_hit_idx"],
            "prev_close": r["prev_close"],
            "gap_pct_at_hit": round(gap, 3),
            "stratum": st,
        })

    print("pool sizes:", {k: len(v) for k, v in pools.items()})

    rng = random.Random(SEED)
    sample = []
    n_excluded_no_ticks = 0
    excluded_list = []
    for st in ("low", "mid", "high"):
        pool = sorted(pools[st], key=lambda x: (x["ticker"], x["date"]))
        rng.shuffle(pool)
        target = min(TARGETS[st], len(pool))
        taken = 0
        for rec in pool:
            if taken >= target:
                break
            td = None
            try:
                td = load_trades(rec["ticker"], rec["date"], rec["mom_pct"])
            except Exception:
                td = None
            if td is None or td.n_trades < 30:
                n_excluded_no_ticks += 1
                excluded_list.append((rec["ticker"], rec["date"], st))
                continue
            sample.append(rec)
            taken += 1
        print(f"  {st}: target={TARGETS[st]} available={len(pool)} taken={taken}")

    # de-dup safety (shouldn't happen — pools are unique keys)
    seen = set()
    final = []
    for r in sample:
        k = (r["ticker"], r["date"])
        if k in seen:
            continue
        seen.add(k)
        final.append(r)

    out = {
        "description": "R3 stratified val sample (seed=42, gap_pct_at_hit strata, MDR>=200 excluded)",
        "seed": SEED,
        "strata_targets": TARGETS,
        "events": final,
    }
    OUT.write_text(json.dumps(out, indent=2))

    counts = {"low": 0, "mid": 0, "high": 0}
    for r in final:
        counts[r["stratum"]] += 1
    print(f"\nFINAL sample: N_total={len(final)}  "
          f"low={counts['low']} mid={counts['mid']} high={counts['high']}  seed={SEED}")
    print(f"excluded (no/insufficient ticks, T1a): {n_excluded_no_ticks} {excluded_list}")
    print(f"written: {OUT}")


if __name__ == "__main__":
    main()
