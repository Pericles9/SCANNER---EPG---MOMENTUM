"""
T2 — Build 300-event training sample and 10-event chart set for Phase EPG-GRT.

Outputs:
  results/phase_epg_grt/train_sample.json    — 300-event stratified sample (seed=42)
  results/phase_epg_grt/chart_events.json    — 10-event representative chart set

Escalation guard: any event date >= 2023-11-17 is a hard stop.

Chart set selection:
  Sort 300 events by peak_intraday_pct (max tick price - prev_close) / prev_close.
  Split into terciles. Pick 4 from top, 3 from middle, 3 from bottom (random within
  each tercile, seed=42).
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import sys
import tempfile
from pathlib import Path

import numpy as np

# ── Path setup ─────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from data.loaders.trades import list_events, load_trades
from data.loaders.prev_close import get_prev_close
from data.schemas.mom_db import CONFIG_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────────────
N_SAMPLE = 300
SEED = 42
VAL_SPLIT_START = "2023-11-17"   # locked; training is everything before this
OUT_DIR = REPO_ROOT / "results" / "phase_epg_grt"

# Chart set allocation by tercile
CHART_ALLOC = {"top": 4, "mid": 3, "bot": 3}


def write_json_atomic(data, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, suffix=".tmp", delete=False
    ) as f:
        json.dump(data, f, indent=2, default=_json_default)
        tmp = Path(f.name)
    os.replace(str(tmp), str(path))
    log.info(f"Written: {path}")


def _json_default(obj):
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    raise TypeError(f"Not serializable: {type(obj)}")


def stratified_sample(events: list[dict], n: int, seed: int) -> list[dict]:
    """Year-proportional stratified sample matching runner.py algorithm exactly."""
    rng = random.Random(seed)

    by_year: dict[str, list] = {}
    for e in events:
        year = e["date"][:4]
        by_year.setdefault(year, []).append(e)

    year_counts = {y: len(evs) for y, evs in by_year.items()}
    total = sum(year_counts.values())
    alloc = {y: int(n * cnt / total) for y, cnt in year_counts.items()}
    remainder = n - sum(alloc.values())
    for y in sorted(year_counts, key=year_counts.get, reverse=True):
        if remainder <= 0:
            break
        alloc[y] += 1
        remainder -= 1

    log.info(f"Year allocation: {dict(sorted(alloc.items()))}")

    sampled = []
    for y in sorted(by_year):
        n_y = min(alloc[y], len(by_year[y]))
        sampled.extend(rng.sample(by_year[y], n_y))

    return sorted(sampled, key=lambda e: (e["date"], e["ticker"]))


def compute_peak_intraday_pct(ev: dict) -> float | None:
    """Load trades + prev_close for one event; return (max_price - prev_close) / prev_close."""
    ticker = ev["ticker"]
    date = ev["date"]
    mom_pct = ev["mom_pct"]

    prev_close = get_prev_close(ticker, date)
    if prev_close is None or math.isnan(prev_close) or prev_close <= 0:
        log.warning(f"  {ticker} {date}: no prev_close, skipping peak_intraday_pct")
        return None

    try:
        td = load_trades(ticker, date, mom_pct, session_filter=True)
    except FileNotFoundError as e:
        log.warning(f"  {ticker} {date}: {e}")
        return None

    if td.n_trades == 0:
        return None

    max_price = float(np.max(td.prices))
    return (max_price - prev_close) / prev_close


def pick_chart_events(
    events_with_peak: list[dict],
    alloc: dict,
    seed: int,
) -> list[dict]:
    """Sort by peak_intraday_pct, split into 3 terciles, sample per alloc."""
    valid = [e for e in events_with_peak if e["peak_intraday_pct"] is not None]
    valid.sort(key=lambda e: e["peak_intraday_pct"])
    n = len(valid)

    t1 = n // 3
    t2 = 2 * n // 3

    bot = valid[:t1]
    mid = valid[t1:t2]
    top = valid[t2:]

    rng = random.Random(seed)
    chosen = []
    for tercile_name, pool, k in [("top", top, alloc["top"]),
                                   ("mid", mid, alloc["mid"]),
                                   ("bot", bot, alloc["bot"])]:
        selected = rng.sample(pool, min(k, len(pool)))
        for ev in selected:
            ev["tercile"] = tercile_name
        chosen.extend(selected)
        log.info(
            f"Chart set — {tercile_name}: {len(selected)} events "
            f"(peak_intraday_pct range "
            f"{min(e['peak_intraday_pct'] for e in pool):.2%} – "
            f"{max(e['peak_intraday_pct'] for e in pool):.2%})"
        )

    return sorted(chosen, key=lambda e: (e["date"], e["ticker"]))


def main():
    # ── Load holdout boundary ──────────────────────────────────────────
    with open(CONFIG_DIR / "holdout_boundary.json") as f:
        boundary = json.load(f)
    val_start = boundary["val_split_start_date"]
    assert val_start == VAL_SPLIT_START, (
        f"Boundary mismatch: expected {VAL_SPLIT_START}, got {val_start}"
    )

    # ── List training events ───────────────────────────────────────────
    all_events = list_events(min_mom=50.0, require_date=True)
    train_events = [e for e in all_events if e["date"] < val_start]
    log.info(f"Training split: {len(train_events)} events (dates < {val_start})")

    # ── T2c escalation check ───────────────────────────────────────────
    bad = [e for e in train_events if e["date"] >= VAL_SPLIT_START]
    if bad:
        log.error(
            f"HARD STOP (T2c): {len(bad)} training events have date >= {VAL_SPLIT_START}. "
            f"First offender: {bad[0]['ticker']} {bad[0]['date']}"
        )
        sys.exit(1)
    log.info("T2c escalation check PASSED: all training events < val_split_start_date")

    # ── Stratified sample (300 events, seed=42) ────────────────────────
    sampled = stratified_sample(train_events, N_SAMPLE, SEED)
    log.info(f"Sampled {len(sampled)} events")

    # ── T2c check on sample ─────────────────────────────────────────────
    bad_sample = [e for e in sampled if e["date"] >= VAL_SPLIT_START]
    if bad_sample:
        log.error(
            f"HARD STOP (T2c): sample contains {len(bad_sample)} events >= {VAL_SPLIT_START}"
        )
        sys.exit(1)

    # ── Build train_sample.json ────────────────────────────────────────
    year_counts_sampled: dict[str, int] = {}
    for e in sampled:
        yr = e["date"][:4]
        year_counts_sampled[yr] = year_counts_sampled.get(yr, 0) + 1

    train_sample = {
        "meta": {
            "n_events": len(sampled),
            "seed": SEED,
            "val_split_start_date": VAL_SPLIT_START,
            "year_counts": dict(sorted(year_counts_sampled.items())),
            "total_train_events": len(train_events),
        },
        "events": [
            {
                "ticker": e["ticker"],
                "date": e["date"],
                "mom_pct": e["mom_pct"],
            }
            for e in sampled
        ],
    }
    write_json_atomic(train_sample, OUT_DIR / "train_sample.json")

    # ── Compute peak_intraday_pct for each sampled event ──────────────
    log.info("Computing peak_intraday_pct for all sampled events...")
    events_with_peak = []
    n_missing = 0
    for i, ev in enumerate(sampled):
        if i % 50 == 0:
            log.info(f"  {i}/{len(sampled)} events processed...")
        pip = compute_peak_intraday_pct(ev)
        if pip is None:
            n_missing += 1
        events_with_peak.append({
            **ev,
            "peak_intraday_pct": pip,
        })

    log.info(
        f"peak_intraday_pct: {len(events_with_peak) - n_missing} computed, "
        f"{n_missing} missing (no prev_close or no trades)"
    )

    # ── T2b: select 10-event chart set ────────────────────────────────
    chart_events_raw = pick_chart_events(events_with_peak, CHART_ALLOC, seed=SEED)

    chart_events = {
        "meta": {
            "n_events": len(chart_events_raw),
            "seed": SEED,
            "alloc": CHART_ALLOC,
            "tercile_split": "by peak_intraday_pct (max tick price)",
        },
        "events": [
            {
                "ticker": e["ticker"],
                "date": e["date"],
                "mom_pct": e["mom_pct"],
                "peak_intraday_pct": e["peak_intraday_pct"],
                "tercile": e["tercile"],
            }
            for e in chart_events_raw
        ],
    }
    write_json_atomic(chart_events, OUT_DIR / "chart_events.json")

    log.info(
        f"\nT2 complete:\n"
        f"  train_sample.json: {len(sampled)} events\n"
        f"  chart_events.json: {len(chart_events_raw)} events\n"
        f"  Year breakdown: {dict(sorted(year_counts_sampled.items()))}"
    )


if __name__ == "__main__":
    main()
