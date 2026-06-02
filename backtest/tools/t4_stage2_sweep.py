"""
T4 — Stage 2 sweep: peak cooling on top 15 Stage 1 configs + s1_t120_po65_pc65.

Reads:
  results/phase_epg_opt2/sweep/stage1_ranked.json  — ranked Stage 1 results

Selects: top 15 non-DQ by Borda + s1_t120_po65_pc65 (regardless of rank).
Runs standard 12-combo cooling grid on all, fine 9-combo grid on t120 only.

Escalation: all Stage 2 cooling configs DQ'd → hard stop.

Writes:
  results/phase_epg_opt2/sweep/stage2_base_configs.json   — selected base configs
  results/phase_epg_opt2/sweep/stage2_{base_id}.json      — per-base results
  results/phase_epg_opt2/sweep/stage2_all.json            — combined (~201 configs)
  results/phase_epg_opt2/sweep/stage2_ranked.json         — Borda ranked
"""
from __future__ import annotations

import json
import logging
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from data.schemas.mom_db import CONFIG_DIR
from tools.sweep_runner_opt2 import (
    build_stage1_configs,
    build_stage2_configs,
    _sweep_worker_opt2,
    aggregate_config_metrics_opt2,
    dq_and_rank,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

TRAIN_SAMPLE_PATH = REPO_ROOT / "results" / "phase_epg_grt" / "train_sample.json"
STAGE1_RANKED_PATH = REPO_ROOT / "results" / "phase_epg_opt2" / "sweep" / "stage1_ranked.json"
OUT_DIR = REPO_ROOT / "results" / "phase_epg_opt2" / "sweep"
N_WORKERS = 8
TOP_N_BASE = 15
T120_BASE_ID = "s1_t120_po65_pc65"


def load_base_configs() -> list[str]:
    """Return top 15 non-DQ Stage 1 config IDs + t120 base."""
    with open(STAGE1_RANKED_PATH) as f:
        ranked = json.load(f)
    non_dq = [r for r in ranked if not r["disqualified"]]
    top15 = [r["config_id"] for r in non_dq[:TOP_N_BASE]]
    if T120_BASE_ID not in top15:
        top15.append(T120_BASE_ID)
        log.info("Added %s to base config list (not in top %d)", T120_BASE_ID, TOP_N_BASE)
    return top15


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with open(TRAIN_SAMPLE_PATH) as f:
        train_data = json.load(f)
    events = train_data["events"]

    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_params = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    base_ids = load_base_configs()
    log.info("Base configs for Stage 2: %d — %s", len(base_ids), base_ids)

    all_stage1 = build_stage1_configs()
    configs = build_stage2_configs(base_ids, all_stage1)
    log.info("Stage 2 cooling configs: %d", len(configs))

    # Write base config list
    base_path = OUT_DIR / "stage2_base_configs.json"
    with open(base_path, "w") as f:
        json.dump({"base_config_ids": base_ids}, f, indent=2)
    log.info("Written: %s", base_path)

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
            "global_fallback_ref": 0.0,
        }
        for e in events
    ]

    log.info("Starting Stage 2 sweep: %d events × %d configs | workers=%d",
             len(events), len(configs), N_WORKERS)

    per_config: dict[str, list[dict]] = {c["config_id"]: [] for c in configs}
    n_ok = n_skip = n_err = 0
    t0 = time.time()

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
                log.warning("Error %s %s: %s", r["ticker"], r["date"], r.get("error", "")[:200])

            done = n_ok + n_skip + n_err
            if done % 50 == 0 or done == len(events):
                log.info("  %d/%d (ok=%d skip=%d err=%d) %.1fs",
                         done, len(events), n_ok, n_skip, n_err, time.time() - t0)

    log.info("Sweep done: ok=%d skip=%d err=%d in %.1fs",
             n_ok, n_skip, n_err, time.time() - t0)

    # Aggregate and write per-base-config files
    all_rows = []
    by_base: dict[str, list[dict]] = {bid: [] for bid in base_ids}

    for cfg in configs:
        cid = cfg["config_id"]
        base_id = cfg["base_config_id"]
        metrics = aggregate_config_metrics_opt2(per_config[cid])
        row = {
            "config_id": cid,
            "base_config_id": base_id,
            "variant": "a",
            "tau": cfg["tau"],
            "p_open": cfg["p_open"],
            "p_close": cfg["p_close"],
            "m_cool_sec": cfg["m_cool_sec"],
            "tau_cool_sec": cfg["tau_cool_sec"],
            **metrics,
        }
        all_rows.append(row)
        by_base[base_id].append(row)

    for base_id, base_rows in by_base.items():
        out_path = OUT_DIR / f"stage2_{base_id}.json"
        with open(out_path, "w") as f:
            json.dump(base_rows, f, indent=2)

    # Write combined
    all_path = OUT_DIR / "stage2_all.json"
    with open(all_path, "w") as f:
        json.dump({"meta": {"n_configs": len(all_rows)}, "configs": all_rows}, f, indent=2)
    log.info("Written: %s", all_path)

    # Escalation
    ranked = dq_and_rank(all_rows)
    if all(r["disqualified"] for r in ranked):
        log.error("T4 ESCALATION: all Stage 2 cooling configs are DQ'd.")
        ranked_path = OUT_DIR / "stage2_ranked.json"
        with open(ranked_path, "w") as f:
            json.dump(ranked, f, indent=2)
        sys.exit(2)

    ranked_path = OUT_DIR / "stage2_ranked.json"
    with open(ranked_path, "w") as f:
        json.dump(ranked, f, indent=2)
    log.info("Written: %s", ranked_path)

    non_dq = [r for r in ranked if not r["disqualified"]]
    log.info("\nStage 2 complete: %d/%d non-DQ", len(non_dq), len(ranked))
    log.info("Top 10 by Borda:")
    for i, r in enumerate(non_dq[:10], 1):
        log.info("  %2d. %s  borda=%d  CF=%.4f  CR=%.6f  PF=%.4f",
                 i, r["config_id"], r["borda_score"],
                 r["capture_fraction"], r["capture_rate"], r["profit_factor"])

    # t120 cooling table
    t120_rows = [r for r in ranked if r["base_config_id"] == T120_BASE_ID]
    if t120_rows:
        log.info("\nt120 cooling table (%d combos):", len(t120_rows))
        log.info("  %-45s  %5s  %5s  %6s  %8s  %6s  %6s  %6s",
                 "config_id", "mcool", "tcool", "PF", "CF", "CR", "n", "borda")
        for r in sorted(t120_rows, key=lambda x: x.get("borda_score") or 9999):
            dq = " [DQ]" if r["disqualified"] else ""
            log.info("  %-45s  %5.0f  %5.0f  %6.4f  %8.4f  %6.6f  %6d  %6s%s",
                     r["config_id"], r["m_cool_sec"], r["tau_cool_sec"],
                     r["profit_factor"], r["capture_fraction"], r["capture_rate"],
                     r["n_trades"], str(r.get("borda_score", "DQ")), dq)


if __name__ == "__main__":
    main()
