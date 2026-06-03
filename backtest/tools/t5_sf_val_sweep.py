"""
T5 — Re-run top-decile configs on val sample (seed=99) with setup filter active.

Reads:
  results/phase_epg_opt2_sf/top_decile_configs.json (~52 configs)
  results/phase_epg_opt2/val_seed99/comparison.json (unfiltered OPT2 val PFs)
  config/holdout_boundary.json (val split bounds)

Builds val_comparison.json: filtered vs unfiltered val PF per config.

Escalation:
  - all configs val PF < 1.20 → hard stop
  - mean pct_entries_blocked > 80% → hard stop

Writes:
  results/phase_epg_opt2_sf/sweep_val_sf.json
  results/phase_epg_opt2_sf/sweep_val_sf_ranked.json
  results/phase_epg_opt2_sf/val_comparison.json
"""
from __future__ import annotations

import gc
import json
import logging
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from data.loaders.trades import list_events
from data.schemas.mom_db import CONFIG_DIR
from tools.sweep_runner_opt2 import (
    _sweep_worker_opt2_sf,
    aggregate_config_metrics_sf,
    dq_and_rank,
    sf_worker_initializer,
)
from tools.t8_val_validate_opt2 import stratified_sample

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

TOP_DECILE_PATH = REPO_ROOT / "results" / "phase_epg_opt2_sf" / "top_decile_configs.json"
OPT2_VAL_COMPARISON = REPO_ROOT / "results" / "phase_epg_opt2" / "val_seed99" / "comparison.json"
OUT_DIR = REPO_ROOT / "results" / "phase_epg_opt2_sf"
COMPUTE_PROFILE_PATH = CONFIG_DIR / "compute_profile.json"

VAL_SEED = 99
VAL_N = 100


def load_compute_profile() -> dict:
    defaults = {
        "max_workers": 4, "worker_nice": 15, "use_ionice": True,
        "event_batch_size": 50, "batch_sleep_ms": 200,
        "inter_config_sleep_ms": 500,
        "cpu_throttle_threshold_pct": 70, "cpu_throttle_sleep_sec": 2,
    }
    if COMPUTE_PROFILE_PATH.exists():
        with open(COMPUTE_PROFILE_PATH) as f:
            defaults.update(json.load(f))
    return defaults


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def load_unfiltered_val_pf() -> dict[str, float]:
    """Map config_id → unfiltered OPT2 val PF (from T8 comparison.json)."""
    if not OPT2_VAL_COMPARISON.exists():
        return {}
    data = json.load(open(OPT2_VAL_COMPARISON))
    return {r["config_id"]: r["profit_factor"] for r in data.get("results", [])}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    profile = load_compute_profile()
    log.info("Compute profile: %s", profile)

    with open(CONFIG_DIR / "holdout_boundary.json") as f:
        boundary = json.load(f)
    val_start = boundary["val_split_start_date"]
    test_start = boundary["test_split_start_date"]

    all_events = list_events()
    val_all = [e for e in all_events if val_start <= e["date"] < test_start]
    val_sample = stratified_sample(val_all, VAL_N, VAL_SEED)
    log.info("Val sample: %d events (seed=%d)", len(val_sample), VAL_SEED)

    # Test leakage guard
    bad = [e for e in val_sample if e["date"] >= test_start]
    if bad:
        log.error("HARD STOP: %d val events >= test_split %s", len(bad), test_start)
        sys.exit(1)

    with open(TOP_DECILE_PATH) as f:
        top_decile = json.load(f)
    log.info("Top-decile configs: %d", len(top_decile))

    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_params = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    rho = hawkes_params.get("rho", 0.99)
    n_workers = profile["max_workers"]
    batch_size = profile["event_batch_size"]
    batch_sleep = profile["batch_sleep_ms"] / 1000.0

    per_config: dict[str, list[dict]] = {c["config_id"]: [] for c in top_decile}
    per_config_unf: dict[str, list[dict]] = {c["config_id"]: [] for c in top_decile}
    n_ok = n_skip = n_err = 0
    t0 = time.time()

    for batch_idx, batch in enumerate(chunks(val_sample, batch_size)):
        work_items = [
            {"ticker": e["ticker"], "date": e["date"], "mom_pct": e["mom_pct"],
             "hawkes_params": hawkes_params, "rho": rho, "rho_E": rho,
             "q_bar_cfg": q_bar_cfg, "configs": top_decile,
             "also_unfiltered": True}
            for e in batch
        ]
        with ProcessPoolExecutor(
            max_workers=n_workers,
            initializer=sf_worker_initializer,
            initargs=(profile["worker_nice"], profile["use_ionice"]),
        ) as executor:
            futures = {executor.submit(_sweep_worker_opt2_sf, item): item for item in work_items}
            for fut in as_completed(futures):
                r = fut.result()
                if r["status"] == "ok":
                    n_ok += 1
                    for cid, ev_res in r["results"].items():
                        per_config[cid].append(ev_res)
                    for cid, ev_res in r.get("results_unfiltered", {}).items():
                        per_config_unf[cid].append(ev_res)
                elif r["status"] == "skipped":
                    n_skip += 1
                else:
                    n_err += 1
                    log.warning("Error %s %s: %s", r["ticker"], r["date"], r.get("error", "")[:150])
        log.info("Batch %d done: ok=%d skip=%d err=%d (%.1fs)",
                 batch_idx + 1, n_ok, n_skip, n_err, time.time() - t0)
        gc.collect()
        time.sleep(batch_sleep)

    log.info("Val sweep done: ok=%d skip=%d err=%d in %.1fs", n_ok, n_skip, n_err, time.time() - t0)

    # Aggregate filtered (SF) and unfiltered (noSF), both on the same 100-event val sample
    rows = []          # SF active
    rows_noSF = []     # SF inactive (apples-to-apples baseline)
    noSF_pf: dict[str, float] = {}
    for cfg in top_decile:
        cid = cfg["config_id"]
        base = {"config_id": cid, "stage": cfg.get("stage", "?"),
                "variant": cfg.get("variant", "a"),
                "tau": cfg.get("tau"), "p_open": cfg.get("p_open"), "p_close": cfg.get("p_close")}
        m = aggregate_config_metrics_sf(per_config[cid])
        m_unf = aggregate_config_metrics_sf(per_config_unf[cid])
        noSF_pf[cid] = m_unf["profit_factor"]
        rows.append({**base, **m})
        rows_noSF.append({**base, **m_unf})
        log.info("  %s: PF_sf=%.4f PF_noSF=%.4f CF=%.4f n=%d blocked=%.1f%% total_pnl=%.1f",
                 cid, m["profit_factor"], m_unf["profit_factor"], m["capture_fraction"],
                 m["n_trades"], m.get("pct_entries_blocked", 0.0), m.get("total_pnl_pct", 0.0))

    with open(OUT_DIR / "sweep_val_sf.json", "w") as f:
        json.dump({"meta": {"n_configs": len(rows), "n_events_ok": n_ok, "val_seed": VAL_SEED},
                   "configs": rows}, f, indent=2)
    log.info("Written: %s", OUT_DIR / "sweep_val_sf.json")

    with open(OUT_DIR / "sweep_val_noSF.json", "w") as f:
        json.dump({"meta": {"n_configs": len(rows_noSF), "n_events_ok": n_ok,
                            "val_seed": VAL_SEED, "setup_filter": False},
                   "configs": rows_noSF}, f, indent=2)
    log.info("Written: %s", OUT_DIR / "sweep_val_noSF.json")

    ranked = dq_and_rank(rows)
    with open(OUT_DIR / "sweep_val_sf_ranked.json", "w") as f:
        json.dump(ranked, f, indent=2)
    log.info("Written: %s", OUT_DIR / "sweep_val_sf_ranked.json")

    # Escalation checks
    all_below_120 = all(r["profit_factor"] < 1.20 for r in rows)
    if all_below_120:
        log.error("T5 ESCALATION: all configs val PF < 1.20 with SF.")
        _write_comparison(rows, noSF_pf)
        sys.exit(2)

    mean_blocked = sum(r.get("pct_entries_blocked", 0.0) for r in rows) / len(rows) if rows else 0.0
    if mean_blocked > 60.0:
        log.error("T5 ESCALATION: mean pct_entries_blocked=%.1f%% > 60%%.", mean_blocked)
        _write_comparison(rows, noSF_pf)
        sys.exit(2)

    _write_comparison(rows, noSF_pf)

    non_dq = [r for r in ranked if not r["disqualified"]]
    log.info("\nT5 complete: %d/%d non-DQ. Mean blocked=%.1f%%. Top 10:",
             len(non_dq), len(ranked), mean_blocked)
    for i, r in enumerate(non_dq[:10], 1):
        log.info("  %2d. %s  borda=%s  PF=%.4f  CF=%.4f  blocked=%.1f%%",
                 i, r["config_id"], r.get("borda_score"), r["profit_factor"],
                 r["capture_fraction"], r.get("pct_entries_blocked", 0.0))


def _write_comparison(rows: list[dict], noSF_pf: dict[str, float]) -> None:
    """Build val comparison: SF vs no-SF on the same seed=99 100-event sample.
    delta_pf = val_pf_sf - val_pf_noSF (apples-to-apples)."""
    comparison = []
    deltas = []
    for r in rows:
        cid = r["config_id"]
        pf_noSF = noSF_pf.get(cid)
        delta = round(r["profit_factor"] - pf_noSF, 4) if pf_noSF is not None else None
        if delta is not None:
            deltas.append(delta)
        comparison.append({
            "config_id": cid,
            "stage": r.get("stage", "?"),
            "val_pf_noSF": pf_noSF,
            "val_pf_sf": r["profit_factor"],
            "delta_pf": delta,
            "val_cf_sf": r["capture_fraction"],
            "val_cr_sf": r["capture_rate"],
            "val_total_pnl_pct": r.get("total_pnl_pct", 0.0),
            "val_n_trades_sf": r["n_trades"],
            "pct_entries_blocked": r.get("pct_entries_blocked", 0.0),
        })
    comparison.sort(key=lambda x: x["delta_pf"] if x["delta_pf"] is not None else -999, reverse=True)
    mean_delta = round(sum(deltas) / len(deltas), 4) if deltas else None
    mean_blocked = round(sum(r.get("pct_entries_blocked", 0.0) for r in rows) / len(rows), 2) if rows else 0.0
    with open(OUT_DIR / "val_comparison.json", "w") as f:
        json.dump({"meta": {"mean_delta_pf": mean_delta, "mean_pct_entries_blocked": mean_blocked,
                            "n_configs": len(comparison)},
                   "configs": comparison}, f, indent=2)
    log.info("Written: %s (mean delta_pf=%s, mean blocked=%.1f%%)",
             OUT_DIR / "val_comparison.json", mean_delta, mean_blocked)


if __name__ == "__main__":
    main()
