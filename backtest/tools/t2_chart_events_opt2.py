"""
T2 — Build 20-event leg-structure chart set for Phase EPG-OPT2.

Reads:
  results/phase_epg_grt/train_sample.json   — 300-event training sample (seed=42)

Classifies each event by PASS window structure using the baseline config
(τ=300, p_open=0.65, p_close=0.65, no cooling). Windows with duration ≥ 90s
are counted per event:
  single_leg     : exactly 1 qualifying window
  multi_leg_2    : exactly 2 qualifying windows
  multi_leg_3plus: 3+ qualifying windows
  failed         : 0 qualifying windows

Selects 5 events per class (seed=7, year-proportional within class).
Hard stop if multi_leg_3plus has fewer than 3 events in the full 300-event sample.

Writes:
  results/phase_epg_opt2/leg_classification.json   — all 300 events classified
  results/phase_epg_opt2/chart_events.json          — 20 selected events
"""
from __future__ import annotations

import json
import logging
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from data.schemas.mom_db import CONFIG_DIR
from tools.t3_sweep_runner import (
    _sweep_worker,
    compute_global_fallback_ref,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

TRAIN_SAMPLE_PATH = REPO_ROOT / "results" / "phase_epg_grt" / "train_sample.json"
OUT_DIR = REPO_ROOT / "results" / "phase_epg_opt2"
LEG_CLASSIFICATION_PATH = OUT_DIR / "leg_classification.json"
CHART_EVENTS_PATH = OUT_DIR / "chart_events.json"

WINDOW_MIN_DURATION_SEC = 90.0
N_PER_CLASS = 5
SEED = 7
MIN_3PLUS_EVENTS = 3
N_WORKERS = 8

# Baseline config: symmetric τ=300, p=0.65, no cooling
BASELINE_CONFIG = {
    "config_id": "baseline_t300_po65_pc65",
    "variant": "a",
    "tau": 300.0,
    "p_open": 0.65,
    "p_close": 0.65,
}


def classify_pass_windows(pass_windows: list[float]) -> tuple[str, int]:
    """Classify an event by its qualifying pass windows."""
    n_qual = sum(1 for w in pass_windows if w >= WINDOW_MIN_DURATION_SEC)
    if n_qual == 0:
        return "failed", n_qual
    elif n_qual == 1:
        return "single_leg", n_qual
    elif n_qual == 2:
        return "multi_leg_2", n_qual
    else:
        return "multi_leg_3plus", n_qual


def stratified_sample_from_class(
    events: list[dict], n: int, seed: int
) -> list[dict]:
    """Year-proportional stratified sample from a class bucket."""
    if len(events) <= n:
        return list(events)
    rng = np.random.default_rng(seed)
    year_buckets: dict[str, list] = {}
    for e in events:
        yr = e["date"][:4]
        year_buckets.setdefault(yr, []).append(e)
    years = sorted(year_buckets.keys())
    total = len(events)
    alloc: dict[str, int] = {}
    allocated = 0
    for yr in years[:-1]:
        cnt = round(n * len(year_buckets[yr]) / total)
        cnt = min(cnt, len(year_buckets[yr]))
        alloc[yr] = cnt
        allocated += cnt
    last_yr = years[-1]
    alloc[last_yr] = min(n - allocated, len(year_buckets[last_yr]))
    allocated += alloc[last_yr]
    if allocated < n:
        for yr in years:
            gap = n - sum(alloc.values())
            if gap <= 0:
                break
            can_add = len(year_buckets[yr]) - alloc[yr]
            add = min(gap, can_add)
            alloc[yr] += add
    sampled = []
    for yr in years:
        bucket = year_buckets[yr]
        k = alloc[yr]
        if k > 0:
            idx = rng.choice(len(bucket), size=k, replace=False)
            sampled.extend(bucket[i] for i in sorted(idx))
    return sampled


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with open(TRAIN_SAMPLE_PATH) as f:
        train_data = json.load(f)
    events = train_data["events"]
    log.info("Loaded %d training events", len(events))

    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_params = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    rho = hawkes_params.get("rho", 0.99)
    configs = [BASELINE_CONFIG]

    # Pre-scan: compute global_fallback_ref (not needed for baseline variant A,
    # but _sweep_worker expects it)
    log.info("Pre-scan: computing global_fallback_ref from %d events...", len(events))
    global_fallback_ref = compute_global_fallback_ref(
        events, hawkes_params, n_workers=N_WORKERS
    )
    log.info("global_fallback_ref=%.6f", global_fallback_ref)

    # Build work items
    work_items = [
        {
            "ticker": e["ticker"],
            "date": e["date"],
            "mom_pct": e["mom_pct"],
            "hawkes_params": hawkes_params,
            "rho": rho,
            "rho_E": rho,
            "q_bar_cfg": q_bar_cfg,
            "configs": configs,
            "global_fallback_ref": global_fallback_ref,
        }
        for e in events
    ]

    log.info("Classifying %d events (baseline gate, %d workers)...", len(events), N_WORKERS)
    t0 = time.time()
    n_ok = n_skip = n_err = 0

    # Map from (ticker, date) → pass_windows
    event_results: dict[tuple, list[float]] = {}

    with ProcessPoolExecutor(max_workers=N_WORKERS) as executor:
        futures = {executor.submit(_sweep_worker, item): item for item in work_items}
        for fut in as_completed(futures):
            r = fut.result()
            if r["status"] == "ok":
                n_ok += 1
                baseline_r = r["results"].get(BASELINE_CONFIG["config_id"], {})
                event_results[(r["ticker"], r["date"])] = baseline_r.get("pass_windows", [])
            elif r["status"] == "skipped":
                n_skip += 1
                event_results[(r["ticker"], r["date"])] = []
            else:
                n_err += 1
                log.warning("Error %s %s: %s", r["ticker"], r["date"], r.get("error", "")[:200])
                event_results[(r["ticker"], r["date"])] = []

            done = n_ok + n_skip + n_err
            if done % 50 == 0 or done == len(events):
                log.info("  %d/%d (ok=%d skip=%d err=%d) %.1fs",
                         done, len(events), n_ok, n_skip, n_err, time.time() - t0)

    log.info("Sweep done: ok=%d skip=%d err=%d in %.1fs", n_ok, n_skip, n_err, time.time() - t0)

    # Classify
    classifications = []
    class_counts = {"single_leg": 0, "multi_leg_2": 0, "multi_leg_3plus": 0, "failed": 0}

    for ev in events:
        key = (ev["ticker"], ev["date"])
        pass_windows = event_results.get(key, [])
        leg_class, n_qual = classify_pass_windows(pass_windows)
        class_counts[leg_class] += 1
        classifications.append({
            "ticker": ev["ticker"],
            "date": ev["date"],
            "leg_class": leg_class,
            "n_windows_ge90s": n_qual,
            "n_windows_total": len(pass_windows),
            "mom_pct": ev.get("mom_pct", 0.0),
        })

    log.info("Class distribution across %d events:", len(events))
    for cls, cnt in sorted(class_counts.items()):
        log.info("  %s: %d (%.1f%%)", cls, cnt, cnt / len(events) * 100)

    # Escalation check
    n_3plus = class_counts.get("multi_leg_3plus", 0)
    if n_3plus < MIN_3PLUS_EVENTS:
        log.error(
            "T2 ESCALATION: multi_leg_3plus has only %d events (< %d). "
            "Post leg class distribution and await instruction.",
            n_3plus, MIN_3PLUS_EVENTS
        )
        with open(LEG_CLASSIFICATION_PATH, "w") as f:
            json.dump({"classifications": classifications, "class_counts": class_counts}, f, indent=2)
        log.info("Written: %s", LEG_CLASSIFICATION_PATH)
        sys.exit(2)

    # Write full classification
    with open(LEG_CLASSIFICATION_PATH, "w") as f:
        json.dump({"classifications": classifications, "class_counts": class_counts}, f, indent=2)
    log.info("Written: %s", LEG_CLASSIFICATION_PATH)

    # Select 5 per class (year-proportional, seed=7)
    by_class: dict[str, list] = {
        "single_leg": [], "multi_leg_2": [], "multi_leg_3plus": [], "failed": []
    }
    for c in classifications:
        by_class[c["leg_class"]].append(c)

    chart_events = []
    for leg_class in ["single_leg", "multi_leg_2", "multi_leg_3plus", "failed"]:
        bucket = by_class[leg_class]
        selected = stratified_sample_from_class(bucket, N_PER_CLASS, SEED)
        if len(selected) < N_PER_CLASS:
            log.warning("Class %s: only %d available (wanted %d)",
                        leg_class, len(selected), N_PER_CLASS)
        for ev in selected:
            chart_events.append({
                "ticker": ev["ticker"],
                "date": ev["date"],
                "leg_class": leg_class,
                "n_windows_ge90s": ev["n_windows_ge90s"],
                "mom_pct": ev.get("mom_pct", 0.0),
            })
        log.info("  %s: selected %d → %s",
                 leg_class, len(selected),
                 [(e["ticker"], e["date"]) for e in selected])

    with open(CHART_EVENTS_PATH, "w") as f:
        json.dump(chart_events, f, indent=2)
    log.info("Written: %s", CHART_EVENTS_PATH)
    log.info("\nT2 complete: %d chart events", len(chart_events))


if __name__ == "__main__":
    main()
