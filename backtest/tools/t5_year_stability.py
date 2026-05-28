"""
T5 — Year-stability check on top 10 configs from T4 Borda ranking.

Reads:
  results/phase_epg_grt/ranked_all.json   — top 10 non-DQ'd configs
  results/phase_epg_grt/train_sample.json — all 300 training events

Re-runs each config against each year-subset of the training sample using the
same sweep worker as T3. Aggregates per-year PF, n_trades, win_rate, mean_pnl_pct.

Writes:
  results/phase_epg_grt/year_stability.json
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

# Import sweep worker and config builder from T3
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
TRAIN_SAMPLE_PATH = OUT_DIR / "train_sample.json"

TOP_N = 10
YEARS = [2020, 2021, 2022, 2023]
N_WORKERS = 8


def load_top10() -> list[dict]:
    path = OUT_DIR / "ranked_all.json"
    if not path.exists():
        log.error("ranked_all.json not found — run T4 first")
        sys.exit(1)
    with open(path) as f:
        data = json.load(f)
    rows = data if isinstance(data, list) else data.get("configs", [])
    qualified = [r for r in rows if not r.get("disqualified", False)]
    if not qualified:
        log.error("No qualified configs in ranked_all.json")
        sys.exit(1)
    return qualified[:TOP_N]


def load_hawkes_and_q_bar() -> tuple[dict, dict]:
    from data.schemas.mom_db import CONFIG_DIR
    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hp = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        qb = json.load(f)
    return hp, qb


def build_config_subset(top10: list[dict]) -> list[dict]:
    """Build config dicts for the top 10 configs only."""
    all_cfgs = build_configs()
    top_ids = {r["config_id"] for r in top10}
    return [c for c in all_cfgs if c["config_id"] in top_ids]


def run_year_subset(
    year: int,
    events: list[dict],
    configs: list[dict],
    hawkes_params: dict,
    q_bar_cfg: dict,
    global_fallback_ref: float,
    n_workers: int,
) -> dict[str, dict]:
    """Run all configs against year-filtered events. Returns {config_id: metrics}."""
    year_events = [e for e in events if e["date"].startswith(str(year))]
    if not year_events:
        log.warning("Year %d: no events in training sample", year)
        return {}

    log.info("Year %d: %d events", year, len(year_events))
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
        for e in year_events
    ]

    per_config: dict[str, list[dict]] = {c["config_id"]: [] for c in configs}
    n_ok = n_skip = n_err = 0

    actual_workers = min(n_workers, len(work_items))
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

    log.info("  Year %d done: ok=%d skip=%d err=%d", year, n_ok, n_skip, n_err)

    return {
        cid: aggregate_config_metrics(ev_list)
        for cid, ev_list in per_config.items()
    }


def main() -> None:
    top10 = load_top10()
    log.info("Top %d configs to test:", len(top10))
    for i, r in enumerate(top10, 1):
        log.info("  %d. %s (borda=%d)", i, r["config_id"], r.get("borda_score", 0))

    hawkes_params, q_bar_cfg = load_hawkes_and_q_bar()
    configs = build_config_subset(top10)
    log.info("Built %d config dicts", len(configs))

    with open(TRAIN_SAMPLE_PATH) as f:
        train_sample = json.load(f)
    events = train_sample["events"]

    # Check if any configs are Variant B (need global_fallback_ref)
    needs_b = any(c["variant"] == "b" for c in configs)
    if needs_b:
        global_fallback_ref = compute_global_fallback_ref(
            events, hawkes_params, n_workers=N_WORKERS
        )
    else:
        global_fallback_ref = 0.0

    # Per-year stability sweep
    year_results: dict[int, dict[str, dict]] = {}
    for yr in YEARS:
        year_results[yr] = run_year_subset(
            yr, events, configs, hawkes_params, q_bar_cfg,
            global_fallback_ref, N_WORKERS,
        )

    # Assemble output
    output = []
    for row in top10:
        cid = row["config_id"]
        per_year = {}
        for yr in YEARS:
            m = year_results[yr].get(cid, {})
            per_year[str(yr)] = {
                "n_trades": m.get("n_trades", 0),
                "profit_factor": m.get("profit_factor", 0.0),
                "win_rate": m.get("win_rate", 0.0),
                "mean_pnl_pct": m.get("mean_pnl_pct", 0.0),
                "pass_fraction": m.get("pass_fraction", 0.0),
            }

        entry = {
            "config_id": cid,
            "variant": row.get("variant"),
            "borda_score": row.get("borda_score"),
            "overall": {
                "n_trades": row.get("n_trades"),
                "profit_factor": row.get("profit_factor"),
                "win_rate": row.get("win_rate"),
                "mean_pnl_pct": row.get("mean_pnl_pct"),
            },
            "per_year": per_year,
        }
        output.append(entry)

        log.info("%s overall PF=%.4f n=%d", cid, row.get("profit_factor", 0), row.get("n_trades", 0))
        for yr in YEARS:
            m = per_year[str(yr)]
            log.info("  %d: n=%d PF=%.4f wr=%.2f%% pnl=%.4f%%",
                     yr, m["n_trades"], m["profit_factor"], m["win_rate"], m["mean_pnl_pct"])

    out_path = OUT_DIR / "year_stability.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    log.info("Written: %s", out_path)
    log.info("\nT5 complete: year_stability.json with %d configs × %d years", len(output), len(YEARS))


if __name__ == "__main__":
    main()
