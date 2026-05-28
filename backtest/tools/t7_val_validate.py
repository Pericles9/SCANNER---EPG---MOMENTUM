"""
T7 — Validate top 3 + baseline configs on val seed=99 (100-event stratified sample).

Reads:
  results/phase_epg_grt/selection.json   — top 3 + baseline from T4
  config/holdout_boundary.json           — val_split_start_date, test_split_start_date

Samples 100 events from the val split (2023-11-17 to 2024-07-22) using
year-proportional stratified sampling with seed=99.

Runs the sweep for the 4 selected configs (top_1, top_2, top_3, baseline).

Hard stop conditions:
  - All 3 top configs have PF < baseline PF
  - Any top config has n_trades < 100

Writes:
  results/phase_epg_grt/val_validate.json
"""
from __future__ import annotations

import json
import logging
import math
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from data.loaders.trades import list_events
from data.schemas.mom_db import CONFIG_DIR
from tools.t3_sweep_runner import (
    _sweep_worker,
    build_configs,
    aggregate_config_metrics,
    compute_global_fallback_ref,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

OUT_DIR = REPO_ROOT / "results" / "phase_epg_grt"
SELECTION_PATH = OUT_DIR / "selection.json"

VAL_N = 100
VAL_SEED = 99
N_WORKERS = 8
N_TRADES_MIN = 100
BASELINE_ID = "var_a_t300_po65_pc65"


def load_selection() -> list[dict]:
    if not SELECTION_PATH.exists():
        log.error("selection.json not found — run T4 first")
        sys.exit(1)
    with open(SELECTION_PATH) as f:
        return json.load(f)


def load_val_events(val_start: str, val_end: str) -> list[dict]:
    """Load all val-split events (train_start <= date < test_split)."""
    all_events = list_events()
    val_events = [
        e for e in all_events
        if val_start <= e["date"] < val_end
    ]
    log.info("Val split: %d total events (%s to %s)", len(val_events), val_start, val_end)
    return val_events


def stratified_sample(events: list[dict], n: int, seed: int) -> list[dict]:
    """Year-proportional stratified sample matching runner.py algorithm."""
    rng = np.random.default_rng(seed)

    year_buckets: dict[str, list[dict]] = {}
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

    # Top up if rounding left us short
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


def build_config_subset(selection: list[dict]) -> list[dict]:
    """Build config dicts for the selected config IDs."""
    all_cfgs = build_configs()
    sel_ids = {r["config_id"] for r in selection}
    found = [c for c in all_cfgs if c["config_id"] in sel_ids]

    # Verify baseline is included
    baseline_ids = {c["config_id"] for c in found}
    missing = sel_ids - baseline_ids
    if missing:
        log.warning("Config IDs not found in build_configs(): %s", missing)
    return found


def main() -> None:
    selection = load_selection()
    sel_ids = [r["config_id"] for r in selection]
    log.info("Selected configs: %s", sel_ids)

    with open(CONFIG_DIR / "holdout_boundary.json") as f:
        boundary = json.load(f)

    val_start = boundary["val_split_start_date"]
    test_start = boundary["test_split_start_date"]

    # HARD STOP: do not access test split
    log.info("Val split: %s to %s (excl. %s+)", val_start, test_start, test_start)

    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_params = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    # Load and sample val events
    val_all = load_val_events(val_start, test_start)
    val_sample = stratified_sample(val_all, VAL_N, VAL_SEED)
    log.info("Sampled %d val events (seed=%d)", len(val_sample), VAL_SEED)

    # Verify no test split contamination
    bad = [e for e in val_sample if e["date"] >= test_start]
    if bad:
        log.error("HARD STOP: %d val events >= test_split_start_date %s", len(bad), test_start)
        sys.exit(1)

    year_counts = {}
    for e in val_sample:
        yr = e["date"][:4]
        year_counts[yr] = year_counts.get(yr, 0) + 1
    log.info("Year allocation: %s", year_counts)

    # Build configs for selected IDs
    configs = build_config_subset(selection)
    log.info("Running %d configs on %d val events", len(configs), len(val_sample))

    # Compute global_fallback_ref for Variant B if needed
    needs_b = any(c["variant"] == "b" for c in configs)
    if needs_b:
        global_fallback_ref = compute_global_fallback_ref(
            val_sample, hawkes_params, n_workers=N_WORKERS
        )
    else:
        global_fallback_ref = 0.0

    rho = hawkes_params.get("rho", 0.99)
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
        for e in val_sample
    ]

    per_config: dict[str, list[dict]] = {c["config_id"]: [] for c in configs}
    n_ok = n_skip = n_err = 0

    log.info("Starting val sweep: %d events × %d configs | workers=%d",
             len(work_items), len(configs), N_WORKERS)

    import time
    t0 = time.time()
    actual_workers = min(N_WORKERS, len(work_items))
    with ProcessPoolExecutor(max_workers=actual_workers) as executor:
        futures = {executor.submit(_sweep_worker, item): item for item in work_items}
        for fut in as_completed(futures):
            r = fut.result()
            if r["status"] == "ok":
                n_ok += 1
                for cid, ev_res in r["results"].items():
                    per_config[cid].append(ev_res)
            elif r["status"] == "skipped":
                n_skip += 1
            else:
                n_err += 1
                log.warning("Error %s %s: %s", r["ticker"], r["date"], r.get("error", "")[:200])

            done = n_ok + n_skip + n_err
            if done % 25 == 0 or done == len(work_items):
                log.info("  %d/%d events (ok=%d skip=%d err=%d) %.1fs",
                         done, len(work_items), n_ok, n_skip, n_err, time.time() - t0)

    log.info("Val sweep done: ok=%d skip=%d err=%d in %.1fs",
             n_ok, n_skip, n_err, time.time() - t0)

    # Aggregate metrics
    results = {}
    for cfg in configs:
        cid = cfg["config_id"]
        metrics = aggregate_config_metrics(per_config[cid])
        results[cid] = {"config_id": cid, "variant": cfg["variant"], **metrics}
        log.info("  %s: PF=%.4f n=%d wr=%.1f%%", cid, metrics["profit_factor"],
                 metrics["n_trades"], metrics["win_rate"])

    # Identify baseline and top configs
    baseline = results.get(BASELINE_ID)
    top_ids = [r["config_id"] for r in selection if r.get("selection_role", "").startswith("top")]
    top_results = [results[cid] for cid in top_ids if cid in results]

    # T7 hard stop checks
    escalation = False
    if baseline:
        baseline_pf = baseline["profit_factor"]
        all_worse = all(r["profit_factor"] < baseline_pf for r in top_results)
        if all_worse:
            log.error(
                "T7 ESCALATION: All top configs have PF < baseline (baseline PF=%.4f). "
                "Do not proceed to Phase EPG-OPT without manual review.",
                baseline_pf,
            )
            escalation = True

    low_trades = [r for r in top_results if r["n_trades"] < N_TRADES_MIN]
    if low_trades:
        for r in low_trades:
            log.error(
                "T7 ESCALATION: %s has n_trades=%d < %d on val set.",
                r["config_id"], r["n_trades"], N_TRADES_MIN,
            )
        escalation = True

    if not escalation:
        log.info("T7 checks PASSED: at least one top config beats baseline and all have n >= %d.", N_TRADES_MIN)

    # Write output
    output = {
        "meta": {
            "val_seed": VAL_SEED,
            "n_val_events": len(val_sample),
            "val_start": val_start,
            "val_end": test_start,
            "year_allocation": year_counts,
            "escalation": escalation,
        },
        "results": list(results.values()),
        "baseline": baseline,
    }

    out_path = OUT_DIR / "val_validate.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    log.info("Written: %s", out_path)

    if escalation:
        log.error("T7 complete with ESCALATION — review val_validate.json before proceeding.")
        sys.exit(2)
    else:
        log.info("\nT7 complete: val_validate.json — ready for Phase EPG-OPT review.")


if __name__ == "__main__":
    main()
