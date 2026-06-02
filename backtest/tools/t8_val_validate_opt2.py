"""
T8 — Val validation for Phase EPG-OPT2 (seed=99, 100 events).

Configs to validate:
  - Top 3 by Borda across Stage 1+2 combined
  - GRT baseline: s1_t300_po65_pc30 (no cooling)
  - s1_t120_po65_pc65 best cooling combo (Stage 2 Borda winner for t120)
  - s1_t120_po65_pc65 no cooling (direct before/after comparison)
  - Top Stage 3 F_ss config by Borda
  - Top Stage 3 F_sl config by Borda

Val split: 2023-11-17 to 2024-07-23. Test split locked (do not access).

Hard stops:
  - Any val candidate n_trades < 80
  - All Stage 1+2 val candidates below GRT baseline PF (s1_t300_po65_pc30 val PF)

Writes:
  results/phase_epg_opt2/val_seed99/{config_id}_summary.json  (per config)
  results/phase_epg_opt2/val_seed99/{config_id}_trades.parquet  (per-trade)
  results/phase_epg_opt2/val_seed99/comparison.json
"""
from __future__ import annotations

import json
import logging
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from data.loaders.trades import list_events
from data.schemas.mom_db import CONFIG_DIR
from tools.sweep_runner_opt2 import (
    _sweep_worker_opt2,
    aggregate_config_metrics_opt2,
)
from tools.t5_stage3_sweep import _sweep_worker_stage3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

VAL_SEED = 99
VAL_N = 100
GRT_BASELINE_ID = "s1_t300_po65_pc30"
T120_NO_COOL_ID = "s1_t120_po65_pc65"
T120_BASE_ID = "s1_t120_po65_pc65"
N_WORKERS = 8
N_TRADES_MIN = 80

STAGE1_RANKED = REPO_ROOT / "results" / "phase_epg_opt2" / "sweep" / "stage1_ranked.json"
STAGE2_RANKED = REPO_ROOT / "results" / "phase_epg_opt2" / "sweep" / "stage2_ranked.json"
STAGE3_FSS_RANKED = REPO_ROOT / "results" / "phase_epg_opt2" / "sweep" / "stage3_fss_ranked.json"
STAGE3_FSL_RANKED = REPO_ROOT / "results" / "phase_epg_opt2" / "sweep" / "stage3_fsl_ranked.json"
OUT_DIR = REPO_ROOT / "results" / "phase_epg_opt2" / "val_seed99"


def stratified_sample(events: list[dict], n: int, seed: int) -> list[dict]:
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
            add = min(gap, len(year_buckets[yr]) - alloc[yr])
            alloc[yr] += add
    sampled = []
    for yr in years:
        bucket = year_buckets[yr]
        k = alloc[yr]
        if k > 0:
            idx = rng.choice(len(bucket), size=k, replace=False)
            sampled.extend(bucket[i] for i in sorted(idx))
    return sampled


def load_val_configs() -> list[dict]:
    """Assemble up to 8 val configs (deduped by config_id)."""
    configs_by_id: dict[str, dict] = {}

    def _add(cfg: dict) -> None:
        configs_by_id.setdefault(cfg["config_id"], cfg)

    s1 = json.load(open(STAGE1_RANKED)) if STAGE1_RANKED.exists() else []
    s2 = json.load(open(STAGE2_RANKED)) if STAGE2_RANKED.exists() else []

    # Top 3 across Stage 1+2 combined (by Borda)
    combined = sorted(
        [r for r in s1 + s2 if not r.get("disqualified", False)],
        key=lambda x: x.get("borda_score") or 9999,
    )
    for r in combined[:3]:
        _add(r)

    # GRT baseline (no cooling)
    grt_base = next((r for r in s1 if r["config_id"] == GRT_BASELINE_ID), None)
    if grt_base:
        _add(grt_base)

    # t120 best cooling
    t120_cooling = sorted(
        [r for r in s2 if not r.get("disqualified", False)
         and r.get("base_config_id") == T120_BASE_ID],
        key=lambda x: x.get("borda_score") or 9999,
    )
    if t120_cooling:
        _add(t120_cooling[0])

    # t120 no cooling
    t120_no_cool = next((r for r in s1 if r["config_id"] == T120_NO_COOL_ID), None)
    if t120_no_cool:
        _add(t120_no_cool)

    # Top F_ss and F_sl
    for path in [STAGE3_FSS_RANKED, STAGE3_FSL_RANKED]:
        if path.exists():
            ranked = json.load(open(path))
            non_dq = [r for r in ranked if not r.get("disqualified", False)]
            if non_dq:
                _add(non_dq[0])

    return list(configs_by_id.values())


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with open(CONFIG_DIR / "holdout_boundary.json") as f:
        boundary = json.load(f)
    val_start = boundary["val_split_start_date"]
    test_start = boundary["test_split_start_date"]
    log.info("Val split: %s to %s", val_start, test_start)

    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_params = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    all_events = list_events()
    val_all = [e for e in all_events if val_start <= e["date"] < test_start]
    log.info("Val split total: %d events", len(val_all))

    val_sample = stratified_sample(val_all, VAL_N, VAL_SEED)
    log.info("Sampled %d val events (seed=%d)", len(val_sample), VAL_SEED)

    # Hard stop: no test leakage
    bad = [e for e in val_sample if e["date"] >= test_start]
    if bad:
        log.error("HARD STOP: %d events >= test_split_start %s", len(bad), test_start)
        sys.exit(1)

    year_counts = {}
    for e in val_sample:
        yr = e["date"][:4]
        year_counts[yr] = year_counts.get(yr, 0) + 1
    log.info("Year allocation: %s", year_counts)

    configs = load_val_configs()
    log.info("Val configs (%d): %s", len(configs), [c["config_id"] for c in configs])

    rho = hawkes_params.get("rho", 0.99)

    # Separate variant A configs from SlopeGate configs
    var_a_configs = [c for c in configs if c.get("variant", "a") not in ("f_ss", "f_sl")]
    slope_configs = [c for c in configs if c.get("variant", "a") in ("f_ss", "f_sl")]

    per_config: dict[str, list] = {c["config_id"]: [] for c in configs}
    n_ok = n_skip = n_err = 0
    t0 = time.time()

    # Run variant A
    if var_a_configs:
        work_items = [
            {"ticker": e["ticker"], "date": e["date"], "mom_pct": e["mom_pct"],
             "hawkes_params": hawkes_params, "rho": rho, "rho_E": rho,
             "q_bar_cfg": q_bar_cfg, "configs": var_a_configs,
             "global_fallback_ref": 0.0}
            for e in val_sample
        ]
        with ProcessPoolExecutor(max_workers=N_WORKERS) as executor:
            futures = {executor.submit(_sweep_worker_opt2, item): item for item in work_items}
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
                done = n_ok + n_skip + n_err
                if done % 25 == 0 or done == len(val_sample):
                    log.info("  [varA] %d/%d (ok=%d skip=%d err=%d) %.1fs",
                             done, len(val_sample), n_ok, n_skip, n_err, time.time() - t0)

    # Run SlopeGate configs
    if slope_configs:
        n_ok_s = n_skip_s = n_err_s = 0
        work_items_s = [
            {"ticker": e["ticker"], "date": e["date"], "mom_pct": e["mom_pct"],
             "hawkes_params": hawkes_params, "rho": rho, "rho_E": rho,
             "q_bar_cfg": q_bar_cfg, "configs": slope_configs}
            for e in val_sample
        ]
        with ProcessPoolExecutor(max_workers=N_WORKERS) as executor:
            futures = {executor.submit(_sweep_worker_stage3, item): item for item in work_items_s}
            for fut in as_completed(futures):
                r = fut.result()
                if r["status"] == "ok":
                    n_ok_s += 1
                    for cid, ev_res in r["results"].items():
                        per_config[cid].append(ev_res)
                elif r["status"] == "skipped":
                    n_skip_s += 1
                else:
                    n_err_s += 1
                done_s = n_ok_s + n_skip_s + n_err_s
                if done_s % 25 == 0 or done_s == len(val_sample):
                    log.info("  [slope] %d/%d (ok=%d skip=%d err=%d) %.1fs",
                             done_s, len(val_sample), n_ok_s, n_skip_s, n_err_s, time.time() - t0)

    log.info("Val sweep done in %.1fs", time.time() - t0)

    # Aggregate and check
    results = {}
    for cfg in configs:
        cid = cfg["config_id"]
        m = aggregate_config_metrics_opt2(per_config[cid])
        results[cid] = {"config_id": cid, "variant": cfg.get("variant", "a"), **m}
        log.info("  %s: PF=%.4f n=%d CF=%.4f CR=%.6f",
                 cid, m["profit_factor"], m["n_trades"],
                 m["capture_fraction"], m["capture_rate"])

    # Escalation checks
    escalation = False
    thin = [cid for cid, r in results.items() if r["n_trades"] < N_TRADES_MIN]
    if thin:
        for cid in thin:
            log.error("T8 ESCALATION: %s has n_trades=%d < %d", cid, results[cid]["n_trades"], N_TRADES_MIN)
        escalation = True

    grt_baseline = results.get(GRT_BASELINE_ID)
    s12_candidates = [r for cid, r in results.items()
                      if r.get("variant", "a") not in ("f_ss", "f_sl")
                      and cid != GRT_BASELINE_ID]
    if grt_baseline and s12_candidates:
        baseline_pf = grt_baseline["profit_factor"]
        if all(r["profit_factor"] < baseline_pf for r in s12_candidates):
            log.error("T8 ESCALATION: all Stage 1+2 val candidates below GRT baseline PF=%.4f", baseline_pf)
            escalation = True

    stage3_candidates = [r for r in results.values() if r.get("variant") in ("f_ss", "f_sl")]
    if stage3_candidates and all(r["profit_factor"] < 2.0 for r in stage3_candidates):
        log.warning("T8 NOTE: both Stage 3 val configs below PF 2.0 — Stage 3 inconclusive")

    t120_cool = next((r for cid, r in results.items()
                      if "mc" in cid and T120_BASE_ID.split("_pc")[0] in cid), None)
    t120_no_cool_r = results.get(T120_NO_COOL_ID)
    if t120_cool and t120_no_cool_r:
        if t120_cool["profit_factor"] < t120_no_cool_r["profit_factor"]:
            log.warning("T8 NOTE: t120 best cooling PF (%.4f) < t120 no-cooling PF (%.4f) — cooling hurts t120",
                        t120_cool["profit_factor"], t120_no_cool_r["profit_factor"])

    # Write per-config summary files
    for cid, r in results.items():
        out_path = OUT_DIR / f"{cid}_summary.json"
        with open(out_path, "w") as f:
            json.dump({
                "meta": {"val_seed": VAL_SEED, "n_events": len(val_sample),
                         "val_start": val_start, "val_end": test_start,
                         "year_allocation": year_counts},
                "result": r,
            }, f, indent=2)

    # Comparison table
    comparison = {
        "meta": {"val_seed": VAL_SEED, "n_events": len(val_sample),
                 "escalation": escalation},
        "results": list(results.values()),
        "grt_baseline": grt_baseline,
    }
    comp_path = OUT_DIR / "comparison.json"
    with open(comp_path, "w") as f:
        json.dump(comparison, f, indent=2)
    log.info("Written: %s", comp_path)

    if escalation:
        log.error("T8 complete with ESCALATION — review before proceeding.")
        sys.exit(2)
    else:
        log.info("\nT8 complete: comparison.json — ready for Phase EPG-OPT2 review.")


if __name__ == "__main__":
    main()
