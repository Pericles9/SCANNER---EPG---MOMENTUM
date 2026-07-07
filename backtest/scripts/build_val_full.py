"""
Phase VAL-FULL T1 — build the full held-out val pool (val_full.json).

Same candidate universe as build_val_r4.py (do NOT re-stratify, do NOT sample):
  Source:   DATA_ROOT/filtered/scanner_hit_catalog.json (val split, confirmed hits).
  Universe: mom_pct = (event_high / prev_close - 1)*100 >= 50, trades.parquet present,
            date in val split [2023-11-17, 2024-07-23) (test split excluded by range),
            MDR>=200 diagnostic sample (val_mdr150_diagnostic.json) excluded — same
            as val_r4's build universe.
Exclude:  the 100 events already in val_r4_stratified.json (ticker+date) — genuine
          held-out check.
Take:     ALL remaining candidate events (natural, unbalanced distribution).
Stratum:  reuse val_r4 cutpoints [64.76, 95.13] so T4 can cross-tab with matching cells.

Outputs:
  backtest/data/val_full.json                          — event file for the runner
  backtest/results/phase_val_full/pool_definition.md   — T1 report (size, exclusions, missing log)
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

BACKTEST = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKTEST))

from data.schemas.mom_db import DATA_ROOT, FILTERED_DIR  # noqa: E402

VAL_START, VAL_END = "2023-11-17", "2024-07-23"  # test split starts 2024-07-23 (holdout_boundary)

# Tercile cutpoints reused verbatim from val_r4 (do NOT refit)
CUT_LOW = 64.76
CUT_HIGH = 95.13

OUT_EVENTS = BACKTEST / "data" / "val_full.json"
OUT_REPORT = BACKTEST / "results" / "phase_val_full" / "pool_definition.md"
VALR4 = BACKTEST / "data" / "val_r4_stratified.json"
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
    cat = json.load(open(CAT_PATH))

    # ── val_r4's 100 events (the exclusion for the held-out check) ──
    valr4_events = json.load(open(VALR4))["events"]
    valr4_keys = {(e["ticker"], e["date"]) for e in valr4_events}
    print(f"val_r4 events to exclude: {len(valr4_keys)}")

    # ── MDR>=200 diagnostic exclusions (same as val_r4's universe) ──
    try:
        mdr_keys = {(e["ticker"], e["date"]) for e in json.load(open(MDR200))["events"]}
        print(f"MDR>=200 exclusions loaded: {len(mdr_keys)}")
    except Exception as e:
        print(f"WARNING: could not load MDR200 exclusions: {e}")
        mdr_keys = set()

    # ── events_by_key: filtered dirs with trades.parquet present, mom>=50, val split ──
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

    print(f"list_events (val, mom>=50, trades.parquet present): {len(events_by_key)}")

    # ── Walk the catalog, build the candidate universe ──
    candidates: list[dict] = []
    missing_trades: list[dict] = []          # scanner hit + val + mom>=50 but no trades.parquet folder
    n_no_hit = n_out_of_range = n_mdr = n_valr4 = n_miss_price = n_below_50 = 0
    seen_keys: set[tuple[str, str]] = set()

    for key_str, rec in cat.items():
        if rec.get("scanner_hit_ts_ns") is None:
            n_no_hit += 1
            continue
        t, d = rec["ticker"], rec["date"]
        if not (VAL_START <= d < VAL_END):
            n_out_of_range += 1
            continue
        if (t, d) in mdr_keys:
            n_mdr += 1
            continue
        if (t, d) in valr4_keys:
            n_valr4 += 1
            continue
        if (t, d) not in events_by_key:
            # scanner hit in val range but no qualifying trades.parquet folder — log it
            missing_trades.append({"ticker": t, "date": d})
            continue
        pc = rec.get("prev_close")
        shp = rec.get("scanner_hit_price")
        if not pc or not shp:
            n_miss_price += 1
            continue
        mom = events_by_key[(t, d)]
        st = stratum_of(mom)
        if st is None:
            n_below_50 += 1
            continue
        if (t, d) in seen_keys:
            continue
        seen_keys.add((t, d))
        gap = (shp / pc - 1) * 100.0
        candidates.append({
            "ticker": t,
            "date": d,
            "mom_pct": mom,
            "scanner_hit_ts_ns": rec["scanner_hit_ts_ns"],
            "scanner_hit_tod_sec": rec["scanner_hit_tod_sec"],
            "scanner_hit_price": shp,
            "scanner_hit_idx": rec["scanner_hit_idx"],
            "prev_close": pc,
            "gap_pct_at_hit": round(gap, 3),
            "stratum": st,
        })

    candidates.sort(key=lambda x: (x["date"], x["ticker"]))
    counts = {"low": 0, "mid": 0, "high": 0}
    for r in candidates:
        counts[r["stratum"]] += 1

    out = {
        "description": (
            "VAL-FULL held-out val pool (natural/unstratified distribution). Same candidate "
            f"universe as val_r4 (mom>=50, trades present, val split, MDR>=200 excluded), "
            "minus val_r4's 100 events. Stratum via reused val_r4 cutpoints "
            f"[{CUT_LOW},{CUT_HIGH}]."
        ),
        "cutpoints": {"cut_low": CUT_LOW, "cut_high": CUT_HIGH},
        "excludes": {"val_r4": len(valr4_keys), "mdr_ge_200": len(mdr_keys)},
        "n_events": len(candidates),
        "stratum_counts": counts,
        "events": candidates,
    }
    OUT_EVENTS.write_text(json.dumps(out, indent=2))

    # ── Report ──
    OUT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    lines.append("# Phase VAL-FULL — T1 Pool Definition\n")
    lines.append(f"**Pool file:** `backtest/data/val_full.json`  ")
    lines.append(f"**Total pool size (natural distribution): {len(candidates)} events**\n")
    lines.append("## Universe construction\n")
    lines.append("Candidate universe = same as `build_val_r4.py`:")
    lines.append("- Source: `scanner_hit_catalog.json`, confirmed scanner hit (`scanner_hit_ts_ns` not null)")
    lines.append(f"- Val split only: date in `[{VAL_START}, {VAL_END})` — test split (>= {VAL_END}) excluded by range")
    lines.append("- `mom_pct >= 50`, `trades.parquet` present, `prev_close` & `scanner_hit_price` present")
    lines.append("- MDR>=200 diagnostic sample excluded (matches val_r4's universe)")
    lines.append("- **NOT stratified, NOT sampled** — every qualifying event is included\n")
    lines.append("## Exclusion / skip accounting\n")
    lines.append("| Reason | Count |")
    lines.append("|---|---:|")
    lines.append(f"| val_r4 events excluded (held-out check) | {n_valr4} |")
    lines.append(f"| MDR>=200 diagnostic events excluded | {n_mdr} |")
    lines.append(f"| catalog: no scanner hit | {n_no_hit} |")
    lines.append(f"| catalog: date outside val split | {n_out_of_range} |")
    lines.append(f"| missing prev_close / scanner_hit_price | {n_miss_price} |")
    lines.append(f"| mom_pct < 50 (post-lookup) | {n_below_50} |")
    lines.append(f"| **scanner hit in val range but trades.parquet MISSING** | {len(missing_trades)} |")
    lines.append("")
    lines.append(f"Expected sanity check: val_r4 excluded = {n_valr4} (should be 100 — all of val_r4 came from this pool).")
    lines.append(f"Candidate pool that val_r4 sampled from = VAL-FULL ({len(candidates)}) + val_r4 (100) "
                 f"= {len(candidates) + n_valr4}.\n")
    lines.append("## Stratum distribution (natural, unbalanced)\n")
    lines.append("| Stratum | Range (mom_pct) | n | % |")
    lines.append("|---|---|---:|---:|")
    tot = max(1, len(candidates))
    lines.append(f"| low | [50, {CUT_LOW}) | {counts['low']} | {100*counts['low']/tot:.1f}% |")
    lines.append(f"| mid | [{CUT_LOW}, {CUT_HIGH}) | {counts['mid']} | {100*counts['mid']/tot:.1f}% |")
    lines.append(f"| high | [{CUT_HIGH}, inf) | {counts['high']} | {100*counts['high']/tot:.1f}% |")
    lines.append("")
    if missing_trades:
        lines.append("## Missing-file log (excluded)\n")
        lines.append("Events with a confirmed scanner hit in the val range but no qualifying "
                     "`filtered/{TICKER}_{DATE}_{MOM}/trades.parquet` folder:\n")
        for r in missing_trades:
            lines.append(f"- {r['ticker']} {r['date']}")
        lines.append("")
    esc = "**ESCALATION: pool < 300**" if len(candidates) < 300 else "Pool >= 300 (no escalation)."
    lines.append(f"## Escalation check\n\n{esc}\n")
    OUT_REPORT.write_text("\n".join(lines))

    print(f"\nVAL-FULL pool: N={len(candidates)}  low={counts['low']} mid={counts['mid']} high={counts['high']}")
    print(f"missing trades.parquet (logged): {len(missing_trades)}")
    print(f"val_r4 excluded: {n_valr4} | MDR excluded: {n_mdr}")
    print(f"written: {OUT_EVENTS}")
    print(f"report:  {OUT_REPORT}")


if __name__ == "__main__":
    main()
