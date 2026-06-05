"""
T2 — Build quality-filtered event universes for Phase WJI-POC.

Single filter: SF at T_event — Q_tilde[bar_at_T_event] >= 0.65.
No scanner context filters.

Escalation checks (T2h):
  - quality training universe after SF filter < 200 → hard stop
  - quality training sample < 100 → hard stop
  - quality val sample < 40 → hard stop

Outputs (under results/phase_wji_poc/):
  quality_sample_train.json      — 200-event stratified sample (seed=7)
  quality_sample_val.json        — 100-event stratified sample (seed=7)
  quality_filter_summary.json    — per-filter drop counts + distributions
"""
from __future__ import annotations

import gc
import json
import logging
import os
import random
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from data.loaders.trades import list_events
from data.schemas.mom_db import CONFIG_DIR
from tools.phase_wji_poc.common import quality_filter_worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

OUT_DIR = REPO_ROOT / "results" / "phase_wji_poc"
SF_THRESHOLD = 0.65
TRAIN_SAMPLE_N = 200
VAL_SAMPLE_N = 100
SEED = 7
MAX_WORKERS = 10


def write_json_atomic(data, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
    ) as f:
        json.dump(data, f, indent=2, default=_json_default)
        tmp = Path(f.name)
    os.replace(str(tmp), str(path))
    log.info("Written: %s", path)


def _json_default(obj):
    import math
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    raise TypeError(f"Not serializable: {type(obj)}")


def stratified_sample(events: list[dict], n: int, seed: int) -> list[dict]:
    """Year-proportional stratified sample."""
    rng = random.Random(seed)
    by_year: dict[str, list] = {}
    for e in events:
        year = e["date"][:4]
        by_year.setdefault(year, []).append(e)

    year_counts = {y: len(v) for y, v in by_year.items()}
    total = sum(year_counts.values())
    alloc = {y: int(n * cnt / total) for y, cnt in year_counts.items()}
    remainder = n - sum(alloc.values())
    for y in sorted(year_counts, key=year_counts.get, reverse=True):
        if remainder <= 0:
            break
        alloc[y] += 1
        remainder -= 1

    sampled = []
    for y in sorted(by_year):
        n_y = min(alloc[y], len(by_year[y]))
        sampled.extend(rng.sample(by_year[y], n_y))

    return sorted(sampled, key=lambda e: (e["date"], e["ticker"]))




def run_quality_filter(
    events: list[dict],
    hawkes_params: dict,
    q_bar_cfg: dict,
    rho: float,
    label: str,
) -> list[dict]:
    """
    Run Hawkes + SF trajectory for every event in parallel.
    Returns list of result dicts from quality_filter_worker.
    """
    work_items = [
        {
            "ticker": e["ticker"], "date": e["date"], "mom_pct": e["mom_pct"],
            "hawkes_params": hawkes_params, "rho": rho, "q_bar_cfg": q_bar_cfg,
        }
        for e in events
    ]

    results = []
    n_ok = n_skip = n_err = 0
    t0 = time.time()

    BATCH = 300
    for batch_start in range(0, len(work_items), BATCH):
        batch = work_items[batch_start: batch_start + BATCH]
        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futs = {executor.submit(quality_filter_worker, item): item for item in batch}
            for fut in as_completed(futs):
                r = fut.result()
                results.append(r)
                if r["status"] == "ok":
                    n_ok += 1
                elif r["status"] == "skipped":
                    n_skip += 1
                else:
                    n_err += 1
        pct = (batch_start + len(batch)) / len(work_items) * 100
        log.info(
            "%s: %.0f%% done — ok=%d skip=%d err=%d (%.1fs)",
            label, pct, n_ok, n_skip, n_err, time.time() - t0,
        )
        gc.collect()

    log.info(
        "%s: total ok=%d skip=%d err=%d in %.1fs",
        label, n_ok, n_skip, n_err, time.time() - t0,
    )
    return results


def apply_quality_filters(
    results: list[dict],
    label: str,
) -> tuple[list[dict], dict]:
    """
    Apply one quality filter.  Returns (filtered_events, filter_stats).

    Filter 1 — SF at T_event: Q_tilde >= 0.65
    """
    ok_results = [r for r in results if r["status"] == "ok"]
    log.info("%s: %d events with valid T_event", label, len(ok_results))

    after_f1 = []
    n_fail_f1 = 0
    for r in ok_results:
        q = r.get("q_tilde_at_t_event")
        if q is not None and q >= SF_THRESHOLD:
            after_f1.append(r)
        else:
            n_fail_f1 += 1

    log.info("%s: after SF filter (Q_tilde >= %.2f): %d (dropped %d)",
             label, SF_THRESHOLD, len(after_f1), n_fail_f1)

    stats = {
        "n_with_t_event": len(ok_results),
        "n_after_sf_filter": len(after_f1),
        "n_fail_sf_filter": n_fail_f1,
        "sf_threshold": SF_THRESHOLD,
    }
    return after_f1, stats


def build_event_list(filtered: list[dict]) -> list[dict]:
    """Convert filter results to event dicts for sampling."""
    return [
        {"ticker": r["ticker"], "date": r["date"], "mom_pct": r["mom_pct"]}
        for r in filtered
    ]


def year_counts(events: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for e in events:
        yr = e["date"][:4]
        counts[yr] = counts.get(yr, 0) + 1
    return dict(sorted(counts.items()))


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with open(CONFIG_DIR / "holdout_boundary.json") as f:
        boundary = json.load(f)
    val_start = boundary["val_split_start_date"]
    test_start = boundary["test_split_start_date"]

    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_params = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    rho = hawkes_params.get("rho", 0.99)

    all_events = [e for e in list_events() if e.get("has_quotes", False)]
    train_events_raw = [e for e in all_events if e["date"] < val_start]
    val_events_raw = [e for e in all_events if val_start <= e["date"] < test_start]
    log.info(
        "Raw splits: train=%d val=%d (has_quotes=True)",
        len(train_events_raw), len(val_events_raw),
    )

    TRAIN_CACHE = OUT_DIR / ".cache_train_results.json"
    VAL_CACHE = OUT_DIR / ".cache_val_results.json"

    # ── Training split ────────────────────────────────────────────────
    if TRAIN_CACHE.exists():
        log.info("Loading train results from cache: %s", TRAIN_CACHE)
        with open(TRAIN_CACHE) as f:
            train_results = json.load(f)
    else:
        log.info("=== T2a: Processing training split (%d events) ===", len(train_events_raw))
        train_results = run_quality_filter(train_events_raw, hawkes_params, q_bar_cfg, rho, "TRAIN")
        write_json_atomic(train_results, TRAIN_CACHE)
        log.info("Train results cached: %s", TRAIN_CACHE)

    train_filtered, train_stats = apply_quality_filters(train_results, "TRAIN")

    # Escalation T2h
    if train_stats["n_after_sf_filter"] < 200:
        log.error(
            "T2h ESCALATION: quality training universe after SF filter = %d < 200. Hard stop.",
            train_stats["n_after_sf_filter"],
        )
        write_json_atomic({"escalation": "T2h_sf_train_too_small", **train_stats},
                          OUT_DIR / "quality_filter_summary.json")
        sys.exit(2)

    if len(train_filtered) < 100:
        log.error(
            "T2h ESCALATION: quality training sample = %d < 100. Hard stop.",
            len(train_filtered),
        )
        write_json_atomic({"escalation": "T2h_train_too_small", **train_stats},
                          OUT_DIR / "quality_filter_summary.json")
        sys.exit(2)

    # ── Val split ─────────────────────────────────────────────────────
    if VAL_CACHE.exists():
        log.info("Loading val results from cache: %s", VAL_CACHE)
        with open(VAL_CACHE) as f:
            val_results = json.load(f)
    else:
        log.info("=== T2b: Processing val split (%d events) ===", len(val_events_raw))
        val_results = run_quality_filter(val_events_raw, hawkes_params, q_bar_cfg, rho, "VAL")
        write_json_atomic(val_results, VAL_CACHE)
        log.info("Val results cached: %s", VAL_CACHE)

    val_filtered, val_stats = apply_quality_filters(val_results, "VAL")

    if len(val_filtered) < 40:
        log.error(
            "T2h ESCALATION: quality val sample = %d < 40. Hard stop.", len(val_filtered)
        )
        write_json_atomic({"escalation": "T2h_val_too_small", **val_stats},
                          OUT_DIR / "quality_filter_summary.json")
        sys.exit(2)

    # ── Stratified sampling ───────────────────────────────────────────
    train_events = build_event_list(train_filtered)
    val_events = build_event_list(val_filtered)

    train_sample = stratified_sample(
        train_events, min(TRAIN_SAMPLE_N, len(train_events)), SEED
    )
    val_sample = stratified_sample(
        val_events, min(VAL_SAMPLE_N, len(val_events)), SEED
    )

    log.info("Training sample: %d events", len(train_sample))
    log.info("Val sample: %d events", len(val_sample))

    # ── Write outputs ─────────────────────────────────────────────────
    write_json_atomic({
        "meta": {
            "n_events": len(train_sample),
            "seed": SEED,
            "val_split_start_date": val_start,
            "year_counts": year_counts(train_sample),
            "total_quality_universe": len(train_events),
            "filter_stats": train_stats,
        },
        "events": train_sample,
    }, OUT_DIR / "quality_sample_train.json")

    write_json_atomic({
        "meta": {
            "n_events": len(val_sample),
            "seed": SEED,
            "val_split_start_date": val_start,
            "test_split_start_date": test_start,
            "year_counts": year_counts(val_sample),
            "total_quality_universe": len(val_events),
            "filter_stats": val_stats,
        },
        "events": val_sample,
    }, OUT_DIR / "quality_sample_val.json")

    write_json_atomic({
        "train": {
            "raw_events_with_quotes": len(train_events_raw),
            **train_stats,
            "sample_size": len(train_sample),
            "sample_year_counts": year_counts(train_sample),
        },
        "val": {
            "raw_events_with_quotes": len(val_events_raw),
            **val_stats,
            "sample_size": len(val_sample),
            "sample_year_counts": year_counts(val_sample),
        },
        "meta": {
            "sf_threshold": SF_THRESHOLD,
            "train_sample_seed": SEED,
            "val_sample_seed": SEED,
        },
    }, OUT_DIR / "quality_filter_summary.json")

    log.info(
        "\nT2 complete:\n"
        "  Train quality universe: %d events → sample: %d\n"
        "  Val quality universe:   %d events → sample: %d",
        len(train_events), len(train_sample),
        len(val_events), len(val_sample),
    )


if __name__ == "__main__":
    main()
