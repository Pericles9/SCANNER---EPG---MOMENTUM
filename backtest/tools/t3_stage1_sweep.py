"""
T3 — Stage 1 sweep: 84 level-gate configs on 300-event training sample.

No cooling. p_close floor extended to 0.15.

Reads:
  results/phase_epg_grt/train_sample.json  — 300-event training sample (seed=42)

All Stage 1 configs are run fresh (EPG-GRT did not record max_price_during_hold).

Escalation checks (post-sweep):
  - All 84 configs DQ'd → hard stop
  - All p_close ≤ 0.20 configs DQ'd across all τ/p_open → hard stop

Writes:
  results/phase_epg_opt2/sweep/stage1_raw.json     — 84 rows, aggregate metrics
  results/phase_epg_opt2/sweep/stage1_ranked.json  — same rows sorted by Borda
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
from tools.t3_sweep_runner import compute_global_fallback_ref
from tools.sweep_runner_opt2 import (
    build_stage1_configs,
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
OUT_DIR = REPO_ROOT / "results" / "phase_epg_opt2" / "sweep"
N_WORKERS = 8


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with open(TRAIN_SAMPLE_PATH) as f:
        train_data = json.load(f)
    events = train_data["events"]
    log.info("Training events: %d", len(events))

    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_params = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    configs = build_stage1_configs()
    log.info("Stage 1 configs: %d", len(configs))

    rho = hawkes_params.get("rho", 0.99)

    # global_fallback_ref unused for variant A but required by worker signature
    global_fallback_ref = 0.0

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

    log.info("Starting Stage 1 sweep: %d events × %d configs | workers=%d",
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
                log.info("  %d/%d events (ok=%d skip=%d err=%d) %.1fs",
                         done, len(events), n_ok, n_skip, n_err, time.time() - t0)

    log.info("Sweep done: ok=%d skip=%d err=%d in %.1fs",
             n_ok, n_skip, n_err, time.time() - t0)

    # Aggregate
    rows = []
    for cfg in configs:
        cid = cfg["config_id"]
        metrics = aggregate_config_metrics_opt2(per_config[cid])
        rows.append({
            "config_id": cid,
            "variant": "a",
            "tau": cfg["tau"],
            "p_open": cfg["p_open"],
            "p_close": cfg["p_close"],
            "m_cool_sec": cfg["m_cool_sec"],
            **metrics,
        })
        log.info("  %s: PF=%.4f CF=%.4f CR=%.6f n=%d pf=%.3f",
                 cid, metrics["profit_factor"], metrics["capture_fraction"],
                 metrics["capture_rate"], metrics["n_trades"], metrics["pass_fraction"])

    # Write raw
    raw_path = OUT_DIR / "stage1_raw.json"
    with open(raw_path, "w") as f:
        json.dump({"meta": {"n_configs": len(rows), "n_events_ok": n_ok}, "configs": rows}, f, indent=2)
    log.info("Written: %s", raw_path)

    # Escalation checks
    all_dq = all(r.get("disqualified", False) for r in rows)  # not yet set; check after ranking
    ranked = dq_and_rank(rows)

    all_dq = all(r["disqualified"] for r in ranked)
    if all_dq:
        log.error("T3 ESCALATION: all 84 Stage 1 configs are DQ'd. "
                  "Post full table and await instruction.")
        ranked_path = OUT_DIR / "stage1_ranked.json"
        with open(ranked_path, "w") as f:
            json.dump(ranked, f, indent=2)
        sys.exit(2)

    low_pc_dq = all(
        r["disqualified"]
        for r in ranked
        if r.get("p_close", 1.0) <= 0.20
    )
    if low_pc_dq:
        log.error("T3 ESCALATION: all p_close ≤ 0.20 configs are DQ'd. "
                  "Post DQ breakdown by p_close and await instruction.")
        ranked_path = OUT_DIR / "stage1_ranked.json"
        with open(ranked_path, "w") as f:
            json.dump(ranked, f, indent=2)
        sys.exit(2)

    # Write ranked
    ranked_path = OUT_DIR / "stage1_ranked.json"
    with open(ranked_path, "w") as f:
        json.dump(ranked, f, indent=2)
    log.info("Written: %s", ranked_path)

    # Summary
    non_dq = [r for r in ranked if not r["disqualified"]]
    log.info("\nStage 1 complete: %d/%d non-DQ configs", len(non_dq), len(ranked))
    log.info("Top 10 by Borda:")
    for i, r in enumerate(non_dq[:10], 1):
        log.info("  %2d. %s  borda=%d  CF=%.4f  CR=%.6f  PF=%.4f  n=%d",
                 i, r["config_id"], r["borda_score"],
                 r["capture_fraction"], r["capture_rate"],
                 r["profit_factor"], r["n_trades"])

    # p_close curve for τ=300, p_open=0.65
    log.info("\np_close curve (τ=300, p_open=0.65):")
    log.info("  %-30s  %6s  %8s  %6s  %6s", "config_id", "PF", "CF", "CR", "n")
    for r in ranked:
        if r.get("tau") == 300.0 and r.get("p_open") == 0.65:
            dq_str = " [DQ]" if r["disqualified"] else ""
            log.info("  %-30s  %6.4f  %8.4f  %6.6f  %6d%s",
                     r["config_id"], r["profit_factor"],
                     r["capture_fraction"], r["capture_rate"],
                     r["n_trades"], dq_str)


if __name__ == "__main__":
    main()
