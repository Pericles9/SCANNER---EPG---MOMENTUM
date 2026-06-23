"""
Build MDR>=150 diagnostic sample for EPG-Rapid R0-R4.

Selection criteria:
  - val split only (respects holdout_boundary.json)
  - mom_pct >= 150.0
  - t_scanner_hit_sec IS NOT NULL (confirmed scanner hit in scanner_hit_catalog.json)
  - ORDER BY RANDOM() LIMIT 100
  - Hard stop if universe < 150

Output: DATA_ROOT / "val_mdr150_diagnostic.json"

Sample is frozen on disk — runner loads it as a fixed event list for R0–R4 runs.
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime
from pathlib import Path

BACKTEST = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKTEST))

from data.loaders.trades import list_events
from data.schemas.mom_db import CONFIG_DIR, DATA_ROOT

MDR_MIN = 150.0
SAMPLE_N = 100
UNIVERSE_MIN = 150
SAMPLE_SEED = 0   # fixed so the build is reproducible; different from val seed=42

CATALOG_PATH = DATA_ROOT / "filtered" / "scanner_hit_catalog.json"
OUT_PATH = DATA_ROOT / "val_mdr150_diagnostic.json"


def main() -> None:
    # ── Load val split boundaries ──────────────────────────────────────
    with open(CONFIG_DIR / "holdout_boundary.json") as f:
        boundary = json.load(f)
    val_start = boundary["val_split_start_date"]
    test_start = boundary["test_split_start_date"]
    print(f"Val split: {val_start} to {test_start} (exclusive)")

    # ── Load scanner hit catalog ───────────────────────────────────────
    if not CATALOG_PATH.exists():
        print(f"ERROR: scanner hit catalog not found at {CATALOG_PATH}")
        sys.exit(1)
    with open(CATALOG_PATH) as f:
        catalog: dict = json.load(f)
    print(f"Catalog: {len(catalog)} entries")

    # ── Filter events: val split + mom_pct >= 200 ─────────────────────
    all_events = list_events(min_mom=MDR_MIN, require_date=True)
    val_events = [
        e for e in all_events
        if val_start <= e["date"] < test_start
    ]
    print(f"Val events with mom_pct >= {MDR_MIN}: {len(val_events)}")

    # ── Filter: confirmed scanner hit ─────────────────────────────────
    confirmed: list[dict] = []
    n_no_hit = 0
    n_not_in_catalog = 0
    for e in val_events:
        key = f"{e['ticker']}:{e['date']}"
        rec = catalog.get(key)
        if rec is None:
            n_not_in_catalog += 1
            continue
        if rec.get("scanner_hit_ts_ns") is None:
            n_no_hit += 1
            continue
        confirmed.append({
            **e,
            "scanner_hit_ts_ns": rec["scanner_hit_ts_ns"],
            "scanner_hit_tod_sec": rec.get("scanner_hit_tod_sec"),
            "scanner_hit_price": rec.get("scanner_hit_price"),
            "scanner_hit_idx": rec.get("scanner_hit_idx"),
            "prev_close": rec.get("prev_close"),
        })

    universe_n = len(confirmed)
    print(f"\nUniverse (mom_pct>={MDR_MIN} + confirmed scanner hit): {universe_n}")
    print(f"  No scanner hit in catalog: {n_no_hit}")
    print(f"  Not in catalog:            {n_not_in_catalog}")

    # ── Hard stop ─────────────────────────────────────────────────────
    if universe_n < UNIVERSE_MIN:
        print(
            f"\nHARD STOP: universe size {universe_n} < {UNIVERSE_MIN} minimum. "
            f"Cannot build MDR>={MDR_MIN} sample."
        )
        sys.exit(2)

    print(f"Universe {universe_n} >= {UNIVERSE_MIN} -- proceeding to sample {SAMPLE_N}.")

    # ── Random sample (ORDER BY RANDOM() LIMIT 100) ───────────────────
    rng = random.Random(SAMPLE_SEED)
    sample = rng.sample(confirmed, min(SAMPLE_N, universe_n))
    sample = sorted(sample, key=lambda e: (e["date"], e["ticker"]))

    # ── Print sample stats ────────────────────────────────────────────
    mom_pcts = [e["mom_pct"] for e in sample]
    print(f"\nSample: {len(sample)} events")
    print(f"  mom_pct range: {min(mom_pcts):.0f}% – {max(mom_pcts):.0f}%")
    print(f"  mom_pct median: {sorted(mom_pcts)[len(mom_pcts)//2]:.0f}%")
    by_year: dict[str, int] = {}
    for e in sample:
        y = e["date"][:4]
        by_year[y] = by_year.get(y, 0) + 1
    print(f"  Year distribution: {dict(sorted(by_year.items()))}")

    # ── Write output ──────────────────────────────────────────────────
    output = {
        "_description": (
            f"MDR>={int(MDR_MIN)} diagnostic sample for EPG-Rapid R0-R4. "
            f"{len(sample)} events randomly selected from val split where "
            f"mom_pct >= {MDR_MIN} AND t_scanner_hit_sec IS NOT NULL. "
            "Not stratified. Confirmed scanner hit for every event."
        ),
        "_generation": {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "val_split": f"{val_start} to {test_start}",
            "min_mom_pct": MDR_MIN,
            "universe_n": universe_n,
            "sample_n": len(sample),
            "sample_seed": SAMPLE_SEED,
        },
        "events": sample,
    }

    with open(OUT_PATH, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nWritten to {OUT_PATH}")

    # ── Spot checks ───────────────────────────────────────────────────
    print("\nFirst 5 events in sample:")
    for e in sample[:5]:
        print(
            f"  {e['ticker']} {e['date']} mom={e['mom_pct']:.0f}% "
            f"scanner_hit_tod={e.get('scanner_hit_tod_sec'):.0f}s"
        )
    print("\nLast 5 events in sample:")
    for e in sample[-5:]:
        print(
            f"  {e['ticker']} {e['date']} mom={e['mom_pct']:.0f}% "
            f"scanner_hit_tod={e.get('scanner_hit_tod_sec'):.0f}s"
        )


if __name__ == "__main__":
    main()
