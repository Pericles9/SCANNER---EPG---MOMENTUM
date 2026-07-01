"""
Phase REBUILD-VAL T2 — build the stratified val sample (val_r4_stratified.json).

Source:   DATA_ROOT/filtered/scanner_hit_catalog.json (val split, confirmed hits).
Exclude:  the 100 MDR>=200 diagnostic events (val_mdr150_diagnostic.json).
Stratify: momentum_pct = (event_high / prev_close - 1) * 100  (percentage gain).
  low  [50, 64.76)  target 30   (p33 of candidate pool)
  mid  [64.76, 95.13) target 40 (p33 to p67)
  high [95.13, inf)  target 30  (above p67)
Cutpoints are the p33/p67 tercile boundaries of the 622-event candidate pool.
Sampling: random within stratum, seed=42, no duplicate (ticker,date).
Missing-file check: every sampled event must have loadable trades (n_trades>=30);
    else draw the next event in the shuffled stratum order (record the replacement).
"""
from __future__ import annotations
import json
import random
import re
import sys
from pathlib import Path

BACKTEST = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKTEST))

from data.schemas.mom_db import DATA_ROOT, FILTERED_DIR
from data.loaders.trades import load_trades

VAL_START, VAL_END = "2023-11-17", "2024-07-23"
SEED = 42

# Tercile cutpoints from T1 analysis (p33 and p67 of 622-event candidate pool)
CUT_LOW  = 64.76   # p33
CUT_HIGH = 95.13   # p67

TARGETS = {"low": 30, "mid": 40, "high": 30}
OUT = BACKTEST / "data" / "val_r4_stratified.json"
MDR200 = DATA_ROOT / "val_mdr150_diagnostic.json"
CAT_PATH = FILTERED_DIR / "scanner_hit_catalog.json"

_EVENT_RE = re.compile(
    r"^(?P<ticker>[A-Z0-9.p]+)_(?P<date>\d{4}-\d{2}-\d{2}|None)_(?P<mom>[\d.]+)$"
)


def stratum_of(mom: float) -> str | None:
    if mom < 50.0:
        return None
    if mom < CUT_LOW:
        return "low"
    if mom < CUT_HIGH:
        return "mid"
    return "high"


def main():
    # Load scanner catalog
    cat = json.load(open(CAT_PATH))

    # Load MDR>=200 exclusions
    try:
        excl = {(e["ticker"], e["date"]) for e in json.load(open(MDR200))["events"]}
        print(f"MDR>=200 exclusions loaded: {len(excl)}")
    except Exception as e:
        print(f"WARNING: could not load MDR200 exclusions: {e}")
        excl = set()

    # Build events_by_key from filtered directory (mom_pct from folder name)
    events_by_key: dict[tuple[str, str], float] = {}
    for d in sorted(FILTERED_DIR.iterdir()):
        if not d.is_dir():
            continue
        m = _EVENT_RE.match(d.name)
        if m is None or m.group("date") == "None":
            continue
        mom = float(m.group("mom"))
        if mom < 50.0:
            continue
        if not (d / "trades.parquet").exists():
            continue
        if not (VAL_START <= m.group("date") < VAL_END):
            continue
        events_by_key[(m.group("ticker"), m.group("date"))] = mom

    print(f"list_events (val split, mom>=50, trades.parquet present): {len(events_by_key)}")

    # Build stratified pools from scanner catalog
    pools: dict[str, list[dict]] = {"low": [], "mid": [], "high": []}
    n_no_hit = n_excl = n_miss = n_below_50 = 0
    for key_str, rec in cat.items():
        if rec.get("scanner_hit_ts_ns") is None:
            n_no_hit += 1
            continue
        t, d = rec["ticker"], rec["date"]
        if not (VAL_START <= d < VAL_END):
            continue
        if (t, d) in excl:
            n_excl += 1
            continue
        if (t, d) not in events_by_key:
            continue
        pc  = rec.get("prev_close")
        shp = rec.get("scanner_hit_price")
        if not pc or not shp:
            n_miss += 1
            continue
        mom = events_by_key[(t, d)]
        st = stratum_of(mom)
        if st is None:
            n_below_50 += 1
            continue
        gap = (shp / pc - 1) * 100.0
        pools[st].append({
            "ticker": t,
            "date": d,
            "mom_pct": mom,
            "scanner_hit_ts_ns":  rec["scanner_hit_ts_ns"],
            "scanner_hit_tod_sec": rec["scanner_hit_tod_sec"],
            "scanner_hit_price":  shp,
            "scanner_hit_idx":    rec["scanner_hit_idx"],
            "prev_close":         pc,
            "gap_pct_at_hit":     round(gap, 3),
            "stratum":            st,
        })

    print("pool sizes:", {k: len(v) for k, v in pools.items()})

    # Sample with missing-file check
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

    # De-dup safety
    seen: set[tuple[str, str]] = set()
    final = []
    for r in sample:
        k = (r["ticker"], r["date"])
        if k in seen:
            continue
        seen.add(k)
        final.append(r)

    out = {
        "description": (
            "R4 stratified val sample (seed=42, momentum_pct tercile strata, "
            f"cutpoints=[{CUT_LOW},{CUT_HIGH}], targets=30/40/30, MDR>=200 excluded)"
        ),
        "seed": SEED,
        "cutpoints": {"cut_low": CUT_LOW, "cut_high": CUT_HIGH},
        "strata_targets": TARGETS,
        "events": final,
    }
    OUT.write_text(json.dumps(out, indent=2))

    counts = {"low": 0, "mid": 0, "high": 0}
    for r in final:
        counts[r["stratum"]] += 1
    print(f"\nFINAL sample: N_total={len(final)}  "
          f"low={counts['low']} mid={counts['mid']} high={counts['high']}  seed={SEED}")
    print(f"cutpoints: low<{CUT_LOW}, mid=[{CUT_LOW},{CUT_HIGH}), high>={CUT_HIGH}")
    print(f"excluded (insufficient ticks, <30): {n_excluded_no_ticks} {excluded_list}")
    print(f"written: {OUT}")


if __name__ == "__main__":
    main()
