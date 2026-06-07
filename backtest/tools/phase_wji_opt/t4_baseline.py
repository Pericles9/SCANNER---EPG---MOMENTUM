"""
T4 — Matched λ_V baseline sweep (train, cached).

Runs the identical RunningMaxGate over the λ_V signal for the same train sample
used in T3, same p × hysteresis grid, same corrected hard filters.

T4a: Identify best λ_V config (Borda rank, single-mode only).
T4c: Escalation if best WJI capture_fraction <= best λ_V capture_fraction.

Reads:
  results/phase_wji_poc/quality_sample_train.json
  results/phase_wji_poc/.cache_train_results.json
  results/phase_wji_opt/stage1_wji_rescored.json   (for T4c comparison)

Writes:
  results/phase_wji_opt/stage1_baseline.json
"""
from __future__ import annotations

import gc
import json
import logging
import math
import os
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from data.schemas.mom_db import CONFIG_DIR
from tools.phase_wji_opt.common import build_config_grid, wji_opt_worker, aggregate_config_trades
from tools.phase_wji_opt.scorer import (
    compute_metrics, compute_per_year, apply_hard_filters, borda_rank,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

TRAIN_SAMPLE_PATH = REPO_ROOT / "results" / "phase_wji_poc" / "quality_sample_train.json"
TRAIN_CACHE_PATH = REPO_ROOT / "results" / "phase_wji_poc" / ".cache_train_results.json"
RESCORED_PATH = REPO_ROOT / "results" / "phase_wji_opt" / "stage1_wji_rescored.json"
OUT_PATH = REPO_ROOT / "results" / "phase_wji_opt" / "stage1_baseline.json"
MAX_WORKERS = 10
BATCH_SIZE = 50
TAU_V = 180.0
BETA_SLOW = 0.01
ALPHA = 0.50

CORRECTED_THRESHOLDS = {
    "n_trades_floor": 200,
    "cvar5_floor_pct": -8.0,
    "pf_floor": 1.0,
}


def write_json_atomic(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
    ) as f:
        json.dump(data, f, indent=2, default=_json_default)
        tmp = Path(f.name)
    os.replace(str(tmp), str(path))
    log.info("Written: %s", path)


def _json_default(obj):
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    raise TypeError(f"Not serializable: {type(obj)}")


def load_sample_with_cache(sample_path: Path, cache_path: Path) -> list[dict]:
    with open(sample_path) as f:
        sample = json.load(f)
    with open(cache_path) as f:
        raw_cache = json.load(f)

    cache_index = {}
    for r in raw_cache:
        if r.get("status") == "ok":
            cache_index[(r["ticker"], r["date"])] = r

    enriched = []
    n_missing = 0
    for e in sample["events"]:
        key = (e["ticker"], e["date"])
        cached = cache_index.get(key)
        if cached is None:
            n_missing += 1
            continue
        enriched.append({
            "ticker": e["ticker"],
            "date": e["date"],
            "mom_pct": e["mom_pct"],
            "t_event": cached["t_event"],
            "mu_buy": cached.get("mu_buy", 0.01),
        })

    if n_missing > 0:
        log.warning("load_sample_with_cache: %d events not found in cache (skipped)", n_missing)
    log.info("Loaded %d events (sample=%d, missing=%d)", len(enriched), len(sample["events"]), n_missing)
    return enriched


def run_sweep(events: list[dict], configs: list[dict], q_bar_cfg: dict, label: str) -> list[dict]:
    work_items = [
        {
            "ticker": e["ticker"],
            "date": e["date"],
            "mom_pct": e["mom_pct"],
            "t_event": e["t_event"],
            "mu_buy": e["mu_buy"],
            "q_bar_cfg": q_bar_cfg,
            "configs": configs,
            "signal_type": "lambda_v",
            "alpha": ALPHA,
            "tau_v": TAU_V,
            "beta_slow": BETA_SLOW,
            "use_sf": True,
        }
        for e in events
    ]

    results = []
    n_ok = n_skip = n_err = 0
    t0 = time.time()

    for batch_start in range(0, len(work_items), BATCH_SIZE):
        batch = work_items[batch_start: batch_start + BATCH_SIZE]
        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futs = {executor.submit(wji_opt_worker, item): item for item in batch}
            for fut in as_completed(futs):
                r = fut.result()
                results.append(r)
                s = r.get("status", "error")
                if s == "ok":
                    n_ok += 1
                elif s == "skipped":
                    n_skip += 1
                else:
                    n_err += 1
        pct = (batch_start + len(batch)) / len(work_items) * 100
        log.info("%s: %.0f%% done — ok=%d skip=%d err=%d (%.1fs)",
                 label, pct, n_ok, n_skip, n_err, time.time() - t0)
        gc.collect()

    log.info("%s: total ok=%d skip=%d err=%d in %.1fs", label, n_ok, n_skip, n_err, time.time() - t0)
    return results


def main() -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    if not RESCORED_PATH.exists():
        log.error("T3-RESCORE output not found: %s\nRun t3_rescore.py first.", RESCORED_PATH)
        sys.exit(1)

    with open(RESCORED_PATH) as f:
        rescored = json.load(f)
    wji_winner = rescored["filter_results"].get("winner")
    if wji_winner is None:
        log.error("T3-RESCORE has no winner. Cannot perform T4c comparison.")
        sys.exit(1)
    wji_best_cf = rescored["configs"][wji_winner].get("capture_fraction")
    log.info("T3 WJI winner: %s  capture_fraction=%.4f", wji_winner, wji_best_cf or 0.0)

    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    configs = build_config_grid()
    log.info("Stage 1 grid: %d configs", len(configs))

    events = load_sample_with_cache(TRAIN_SAMPLE_PATH, TRAIN_CACHE_PATH)
    if len(events) < 50:
        log.error("Too few enriched events (%d). Aborting.", len(events))
        sys.exit(2)

    # T4: λ_V sweep
    log.info("=== T4: λ_V signal sweep (%d events) ===", len(events))
    results = run_sweep(events, configs, q_bar_cfg, label="T4-LV")

    # Aggregate
    config_ids = [c["config_id"] for c in configs]
    all_trades = aggregate_config_trades(results, config_ids)
    metrics_by_config: dict[str, dict] = {}
    per_year_by_config: dict[str, dict] = {}
    for cid in config_ids:
        trades = all_trades[cid]
        metrics_by_config[cid] = compute_metrics(trades, CORRECTED_THRESHOLDS)
        per_year_by_config[cid] = compute_per_year(trades)

    # Hard filter + Borda (single-mode only for ranking, per spec T4a)
    survivors = apply_hard_filters(config_ids, metrics_by_config, CORRECTED_THRESHOLDS)
    single_survivors = [cid for cid in survivors if "single" in cid]
    ranked_single = borda_rank(single_survivors, metrics_by_config) if single_survivors else []
    lv_winner = ranked_single[0] if ranked_single else None

    # Build borda rank across all survivors (for reporting)
    all_ranked = borda_rank(survivors, metrics_by_config) if survivors else []
    borda_scores = {cid: i + 1 for i, cid in enumerate(all_ranked)}

    config_meta = {c["config_id"]: {"p": c["p"], "hysteresis": c["hysteresis"]} for c in configs}

    out_configs: dict[str, dict] = {}
    for cid in config_ids:
        m = metrics_by_config[cid]
        meta = config_meta[cid]
        out_configs[cid] = {
            "p": meta["p"],
            "hysteresis": meta["hysteresis"],
            "capture_fraction": m.get("capture_fraction"),
            "ev": m.get("ev"),
            "cvar5_pct": m.get("cvar5_pct"),
            "max_loss_pct": m.get("max_loss_pct"),
            "worst_event": m.get("worst_event"),
            "median_pct": m.get("median_pct"),
            "n_trades": m.get("n_trades"),
            "pf": m.get("pf"),
            "filter_pass": cid in survivors,
            "borda_rank": borda_scores.get(cid),
        }

    output = {
        "meta": {
            "split": "train",
            "signal_type": "lambda_v",
            "n_events": sum(1 for r in results if r.get("status") == "ok"),
            "thresholds": CORRECTED_THRESHOLDS,
        },
        "configs": out_configs,
        "per_year": per_year_by_config,
        "filter_results": {
            "survivors": survivors,
            "ranked_all": all_ranked,
            "single_mode_ranked": ranked_single,
            "winner": lv_winner,
        },
    }
    write_json_atomic(output, OUT_PATH)

    # Log T4 table
    log.info("\nT4 λ_V results:")
    log.info("%-20s %6s %8s %7s %8s %9s %11s %5s %4s",
             "config", "p", "hyst", "cap_fr", "ev", "cvar5", "max_loss", "n", "pf")
    for cid in config_ids:
        c = out_configs[cid]
        flag = "PASS" if c["filter_pass"] else "FAIL"
        rank_str = f"#{borda_scores[cid]}" if cid in borda_scores else "  -"
        log.info("%-20s %6.2f %-8s %7.4f %8.3f %9.2f %11.2f %5d %4.3f  [%s %s]",
                 cid,
                 c["p"] or 0.0,
                 c["hysteresis"] or "",
                 c["capture_fraction"] or 0.0,
                 c["ev"] or 0.0,
                 c["cvar5_pct"] or 0.0,
                 c["max_loss_pct"] or 0.0,
                 c["n_trades"] or 0,
                 c["pf"] or 0.0,
                 flag, rank_str)

    log.info("T4 λ_V winner (single-mode): %s", lv_winner)

    # T4a / T4c
    if lv_winner:
        lv_best_cf = metrics_by_config[lv_winner].get("capture_fraction")
        log.info(
            "T4c check: WJI %s capture_fraction=%.4f  vs  λ_V %s capture_fraction=%.4f",
            wji_winner, wji_best_cf or 0.0, lv_winner, lv_best_cf or 0.0,
        )
        if wji_best_cf is not None and lv_best_cf is not None:
            if wji_best_cf <= lv_best_cf:
                log.error(
                    "ESCALATION T4c: best WJI capture_fraction (%.4f) <= best λ_V (%.4f). "
                    "WJI signal adds nothing over λ_V alone. Hard stop.",
                    wji_best_cf, lv_best_cf,
                )
                sys.exit(3)
            else:
                log.info("T4c PASS: WJI beats λ_V on capture_fraction (%.4f > %.4f).",
                         wji_best_cf, lv_best_cf)
    else:
        log.warning("T4a: no single-mode λ_V survivor. T4c comparison skipped.")

    log.info("T4 complete. Proceed to T6 (val confirmation).")


if __name__ == "__main__":
    main()
