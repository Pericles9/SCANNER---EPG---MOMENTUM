"""
T4 — Re-run top-decile configs on training sample with setup filter active.

Reads: results/phase_epg_opt2_sf/top_decile_configs.json (~52 configs)
       results/phase_epg_grt/train_sample.json (300 events)

Resource limits from config/compute_profile.json:
  max_workers=4, nice=15, ionice class 3, batch_size=50, sleep between batches+configs.

Escalation: if all ~52 configs DQ'd → hard stop.

Writes:
  results/phase_epg_opt2_sf/sweep_train_sf.json
  results/phase_epg_opt2_sf/sweep_train_sf_ranked.json
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

from data.schemas.mom_db import CONFIG_DIR
from tools.sweep_runner_opt2 import (
    _sweep_worker_opt2_sf,
    aggregate_config_metrics_sf,
    dq_and_rank,
    sf_worker_initializer,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

TRAIN_SAMPLE_PATH = REPO_ROOT / "results" / "phase_epg_grt" / "train_sample.json"
TOP_DECILE_PATH = REPO_ROOT / "results" / "phase_epg_opt2_sf" / "top_decile_configs.json"
OUT_DIR = REPO_ROOT / "results" / "phase_epg_opt2_sf"
COMPUTE_PROFILE_PATH = CONFIG_DIR / "compute_profile.json"


def load_compute_profile() -> dict:
    defaults = {
        "max_workers": 4, "worker_nice": 15, "use_ionice": True,
        "event_batch_size": 50, "batch_sleep_ms": 200,
        "inter_config_sleep_ms": 500,
        "cpu_throttle_threshold_pct": 70, "cpu_throttle_sleep_sec": 2,
    }
    if COMPUTE_PROFILE_PATH.exists():
        with open(COMPUTE_PROFILE_PATH) as f:
            profile = json.load(f)
        defaults.update(profile)
    return defaults


def chunks(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def throttle_if_busy(cpu_threshold: float, sleep_sec: float) -> None:
    try:
        import psutil
        cpu_pct = psutil.cpu_percent(interval=0.1)
        if cpu_pct > cpu_threshold:
            log.info("CPU %.1f%% > threshold %.1f%% — sleeping %.1fs", cpu_pct, cpu_threshold, sleep_sec)
            time.sleep(sleep_sec)
    except ImportError:
        pass


def log_resource_usage(config_id: str) -> None:
    try:
        import psutil
        proc = psutil.Process()
        cpu = psutil.cpu_percent(interval=0.1)
        mem_gb = proc.memory_info().rss / 1e9
        log.info("[%s] CPU%%=%.1f MEM=%.2fGB", config_id, cpu, mem_gb)
    except ImportError:
        pass


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    profile = load_compute_profile()
    log.info("Compute profile: %s", profile)

    with open(TRAIN_SAMPLE_PATH) as f:
        train_data = json.load(f)
    events = train_data["events"]
    log.info("Training events: %d", len(events))

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
    inter_config_sleep = profile["inter_config_sleep_ms"] / 1000.0

    per_config: dict[str, list[dict]] = {c["config_id"]: [] for c in top_decile}
    sf_event_stats: list[dict] = []

    log.info("Starting T4 sweep: %d events × %d configs | workers=%d batch=%d",
             len(events), len(top_decile), n_workers, batch_size)
    t0 = time.time()
    n_ok = n_skip = n_err = 0

    for batch_idx, batch in enumerate(chunks(events, batch_size)):
        work_items = [
            {
                "ticker": e["ticker"], "date": e["date"], "mom_pct": e["mom_pct"],
                "hawkes_params": hawkes_params, "rho": rho, "rho_E": rho,
                "q_bar_cfg": q_bar_cfg, "configs": top_decile,
            }
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
                    sf_event_stats.append({
                        "ticker": r["ticker"], "date": r["date"],
                        "sf_first_qualify_bar": r.get("sf_first_qualify_bar", -1),
                        "sf_n_bars": r.get("sf_n_bars", 0),
                        "sf_mean_q_tilde": r.get("sf_mean_q_tilde", 0.0),
                    })
                elif r["status"] == "skipped":
                    n_skip += 1
                else:
                    n_err += 1
                    log.warning("Error %s %s: %s", r["ticker"], r["date"], r.get("error", "")[:200])

        log.info("Batch %d/%d done: ok=%d skip=%d err=%d (%.1fs)",
                 batch_idx + 1, (len(events) + batch_size - 1) // batch_size,
                 n_ok, n_skip, n_err, time.time() - t0)
        gc.collect()
        time.sleep(batch_sleep)

    log.info("Sweep done: ok=%d skip=%d err=%d in %.1fs", n_ok, n_skip, n_err, time.time() - t0)

    # Aggregate per-config
    rows = []
    for cfg in top_decile:
        cid = cfg["config_id"]
        throttle_if_busy(profile["cpu_throttle_threshold_pct"], profile["cpu_throttle_sleep_sec"])
        log_resource_usage(cid)

        metrics = aggregate_config_metrics_sf(per_config[cid])
        row = {
            "config_id": cid,
            "stage": cfg.get("stage", "?"),
            "variant": cfg.get("variant", "a"),
            "tau": cfg.get("tau"),
            "p_open": cfg.get("p_open"),
            "p_close": cfg.get("p_close"),
            **metrics,
        }
        rows.append(row)
        log.info("  %s: PF=%.4f CF=%.4f CR=%.6f n=%d blocked=%.1f%%",
                 cid, metrics["profit_factor"], metrics["capture_fraction"],
                 metrics["capture_rate"], metrics["n_trades"],
                 metrics.get("pct_entries_blocked", 0.0))
        time.sleep(inter_config_sleep)

    # Write raw
    raw_path = OUT_DIR / "sweep_train_sf.json"
    with open(raw_path, "w") as f:
        json.dump({"meta": {"n_configs": len(rows), "n_events_ok": n_ok}, "configs": rows}, f, indent=2)
    log.info("Written: %s", raw_path)

    # DQ + rank
    ranked = dq_and_rank(rows)

    # Escalation
    if all(r["disqualified"] for r in ranked):
        log.error("T4 ESCALATION: all top-decile configs DQ'd on training with SF.")
        with open(OUT_DIR / "sweep_train_sf_ranked.json", "w") as f:
            json.dump(ranked, f, indent=2)
        sys.exit(2)

    with open(OUT_DIR / "sweep_train_sf_ranked.json", "w") as f:
        json.dump(ranked, f, indent=2)
    log.info("Written: %s", OUT_DIR / "sweep_train_sf_ranked.json")

    non_dq = [r for r in ranked if not r["disqualified"]]
    log.info("\nT4 complete: %d/%d non-DQ. Top 10:", len(non_dq), len(ranked))
    for i, r in enumerate(non_dq[:10], 1):
        log.info("  %2d. %s  borda=%s  PF=%.4f  CF=%.4f  CR=%.6f  blocked=%.1f%%",
                 i, r["config_id"], r.get("borda_score"), r["profit_factor"],
                 r["capture_fraction"], r["capture_rate"],
                 r.get("pct_entries_blocked", 0.0))


if __name__ == "__main__":
    main()
