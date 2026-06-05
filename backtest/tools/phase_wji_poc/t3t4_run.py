"""
T3/T4 — Run WJI POC + GRT baseline on quality training (T3) and val (T4) samples.

Reads:
  results/phase_wji_poc/quality_sample_train.json  (T3)
  results/phase_wji_poc/quality_sample_val.json    (T4)
  config/phase_wji_poc/wji_poc.json                — WJI POC config
  config/phase_epg_grt/var_a_t300_po65_pc30.json  — GRT baseline config

Writes (T3):
  results/phase_wji_poc/train_wji.json
  results/phase_wji_poc/train_baseline.json

Writes (T4):
  results/phase_wji_poc/val_wji.json
  results/phase_wji_poc/val_baseline.json

Escalation checks:
  T3f: WJI training PF < 1.20 → hard stop
  T3f: component_balance < 0.10 → hard stop
  T4e: WJI val PF < 1.00 → hard stop
  T4e: n_trades_val < 30 → hard stop
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
from tools.sweep_runner_opt2 import aggregate_config_metrics_sf
from tools.phase_wji_poc.common import wji_poc_worker, aggregate_wji_metrics

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

OUT_DIR = REPO_ROOT / "results" / "phase_wji_poc"
WJI_CONFIG_PATH = REPO_ROOT / "config" / "phase_wji_poc" / "wji_poc.json"
BASELINE_CONFIG_PATH = REPO_ROOT / "config" / "phase_epg_grt" / "var_a_t300_po65_pc30.json"
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
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    raise TypeError(f"Not serializable: {type(obj)}")


def load_wji_config() -> dict:
    with open(WJI_CONFIG_PATH) as f:
        raw = json.load(f)
    wji_params = raw["wji"]
    return {
        "config_id": raw["config_id"],
        "variant": "wji",
        "alpha": wji_params["alpha"],
        "tau_v": float(wji_params["tau_v"]),
        "beta_slow": wji_params["beta_slow"],
        "L_sec": float(wji_params["L_sec"]),
        "tau_decay": float(wji_params["tau_decay"]),
        "p_open": wji_params["p_open"],
        "p_close": wji_params["p_close"],
    }


def load_baseline_config() -> dict:
    with open(BASELINE_CONFIG_PATH) as f:
        raw = json.load(f)
    return {
        "config_id": raw.get("config_id", "var_a_t300_po65_pc30"),
        "variant": "a",
        "tau": float(raw["epg"]["tau"]),
        "p_open": raw["epg"]["p_open"],
        "p_close": raw["epg"]["p_close"],
        "m_cool_sec": 0.0,
        "tau_cool_sec": 120.0,
    }


def run_split(
    sample_events: list[dict],
    hawkes_params: dict,
    q_bar_cfg: dict,
    rho: float,
    wji_cfg: dict,
    baseline_cfg: dict,
    label: str,
) -> tuple[list[dict], list[dict]]:
    """
    Run WJI + baseline on all events in sample.
    Returns (wji_results, baseline_results) as lists of per-event dicts.
    """
    work_items = [
        {
            "ticker": e["ticker"], "date": e["date"], "mom_pct": e["mom_pct"],
            "hawkes_params": hawkes_params, "rho": rho, "q_bar_cfg": q_bar_cfg,
            "wji_cfg": wji_cfg, "baseline_cfg": baseline_cfg,
        }
        for e in sample_events
    ]

    wji_results: list[dict] = []
    baseline_results: list[dict] = []
    n_ok = n_skip = n_err = 0
    t0 = time.time()

    BATCH = 100
    for batch_start in range(0, len(work_items), BATCH):
        batch = work_items[batch_start: batch_start + BATCH]
        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futs = {executor.submit(wji_poc_worker, item): item for item in batch}
            for fut in as_completed(futs):
                r = fut.result()
                if r["status"] == "ok":
                    n_ok += 1
                    wji_results.append(r["wji"])
                    baseline_results.append(r["baseline"])
                elif r["status"] == "skipped":
                    n_skip += 1
                else:
                    n_err += 1
                    log.warning("Error %s %s: %s", r["ticker"], r["date"], r.get("error", "")[:200])
        pct = (batch_start + len(batch)) / len(work_items) * 100
        log.info(
            "%s: %.0f%% done — ok=%d skip=%d err=%d (%.1fs)",
            label, pct, n_ok, n_skip, n_err, time.time() - t0,
        )
        gc.collect()

    log.info("%s: total ok=%d skip=%d err=%d in %.1fs", label, n_ok, n_skip, n_err, time.time() - t0)
    return wji_results, baseline_results


def build_result_json(
    metrics: dict,
    sample_events: list[dict],
    config_id: str,
    split: str,
    n_events_ok: int,
) -> dict:
    return {
        "meta": {
            "config_id": config_id,
            "split": split,
            "n_sample_events": len(sample_events),
            "n_events_ok": n_events_ok,
        },
        **metrics,
    }


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--split", choices=["train", "val", "both"], default="both",
        help="Which split to run (default: both)",
    )
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_params = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    rho = hawkes_params.get("rho", 0.99)
    wji_cfg = load_wji_config()
    baseline_cfg = load_baseline_config()

    log.info("WJI config: %s", wji_cfg)
    log.info("Baseline config: %s", baseline_cfg)

    # ── T3: Training ──────────────────────────────────────────────────
    if args.split in ("train", "both"):
        train_sample_path = OUT_DIR / "quality_sample_train.json"
        if not train_sample_path.exists():
            log.error("quality_sample_train.json not found — run T2 first")
            sys.exit(1)

        with open(train_sample_path) as f:
            train_data = json.load(f)
        train_events = train_data["events"]
        log.info("=== T3: Training split (%d events) ===", len(train_events))

        wji_train, baseline_train = run_split(
            train_events, hawkes_params, q_bar_cfg, rho, wji_cfg, baseline_cfg, "T3-TRAIN"
        )

        wji_train_metrics = aggregate_wji_metrics(wji_train)
        baseline_train_metrics = aggregate_wji_metrics(baseline_train)

        log.info(
            "T3 WJI:      PF=%.4f n=%d win=%.1f%% mean_pnl=%.2f%% component_balance=%.3f pct_decay=%.3f",
            wji_train_metrics["profit_factor"],
            wji_train_metrics["n_trades"],
            wji_train_metrics["win_rate"],
            wji_train_metrics["mean_pnl_pct"],
            wji_train_metrics["component_balance"],
            wji_train_metrics["pct_windows_with_prior_decay"],
        )
        log.info(
            "T3 Baseline: PF=%.4f n=%d win=%.1f%% mean_pnl=%.2f%%",
            baseline_train_metrics["profit_factor"],
            baseline_train_metrics["n_trades"],
            baseline_train_metrics["win_rate"],
            baseline_train_metrics["mean_pnl_pct"],
        )

        write_json_atomic(
            build_result_json(wji_train_metrics, train_events, wji_cfg["config_id"], "train", len(wji_train)),
            OUT_DIR / "train_wji.json",
        )
        write_json_atomic(
            build_result_json(baseline_train_metrics, train_events, baseline_cfg["config_id"], "train", len(baseline_train)),
            OUT_DIR / "train_baseline.json",
        )

        # T3f escalation checks
        wji_pf_train = wji_train_metrics["profit_factor"]
        cb_train = wji_train_metrics["component_balance"]

        if wji_pf_train < 1.20:
            log.error(
                "T3f ESCALATION: WJI training PF=%.4f < 1.20. Hard stop.", wji_pf_train
            )
            log.error("component_balance=%.4f pct_prior_decay=%.4f n_trades=%d",
                      cb_train, wji_train_metrics["pct_windows_with_prior_decay"],
                      wji_train_metrics["n_trades"])
            sys.exit(2)

        if cb_train < 0.10:
            log.error(
                "T3f ESCALATION: component_balance=%.4f < 0.10 on training. "
                "Joint condition not binding. Hard stop.", cb_train
            )
            sys.exit(2)

        log.info("T3 escalation checks PASSED (PF=%.4f >= 1.20, balance=%.4f >= 0.10)",
                 wji_pf_train, cb_train)

    # ── T4: Val ───────────────────────────────────────────────────────
    if args.split in ("val", "both"):
        val_sample_path = OUT_DIR / "quality_sample_val.json"
        if not val_sample_path.exists():
            log.error("quality_sample_val.json not found — run T2 first")
            sys.exit(1)

        with open(val_sample_path) as f:
            val_data = json.load(f)
        val_events = val_data["events"]
        log.info("=== T4: Val split (%d events) ===", len(val_events))

        wji_val, baseline_val = run_split(
            val_events, hawkes_params, q_bar_cfg, rho, wji_cfg, baseline_cfg, "T4-VAL"
        )

        wji_val_metrics = aggregate_wji_metrics(wji_val)
        baseline_val_metrics = aggregate_wji_metrics(baseline_val)

        log.info(
            "T4 WJI:      PF=%.4f n=%d win=%.1f%% mean_pnl=%.2f%% component_balance=%.3f pct_decay=%.3f",
            wji_val_metrics["profit_factor"],
            wji_val_metrics["n_trades"],
            wji_val_metrics["win_rate"],
            wji_val_metrics["mean_pnl_pct"],
            wji_val_metrics["component_balance"],
            wji_val_metrics["pct_windows_with_prior_decay"],
        )
        log.info(
            "T4 Baseline: PF=%.4f n=%d win=%.1f%% mean_pnl=%.2f%%",
            baseline_val_metrics["profit_factor"],
            baseline_val_metrics["n_trades"],
            baseline_val_metrics["win_rate"],
            baseline_val_metrics["mean_pnl_pct"],
        )

        write_json_atomic(
            build_result_json(wji_val_metrics, val_events, wji_cfg["config_id"], "val", len(wji_val)),
            OUT_DIR / "val_wji.json",
        )
        write_json_atomic(
            build_result_json(baseline_val_metrics, val_events, baseline_cfg["config_id"], "val", len(baseline_val)),
            OUT_DIR / "val_baseline.json",
        )

        # T4e escalation checks
        wji_pf_val = wji_val_metrics["profit_factor"]
        n_trades_val = wji_val_metrics["n_trades"]

        if wji_pf_val < 1.00:
            log.error(
                "T4e ESCALATION: WJI val PF=%.4f < 1.00 (actively losing money). Hard stop.",
                wji_pf_val,
            )
            sys.exit(2)

        if n_trades_val < 30:
            log.error(
                "T4e ESCALATION: WJI val n_trades=%d < 30 (too few to interpret). Hard stop.",
                n_trades_val,
            )
            log.error("pass_fraction=%.4f component_balance=%.4f",
                      wji_val_metrics["pass_fraction"], wji_val_metrics["component_balance"])
            sys.exit(2)

        log.info("T4 escalation checks PASSED (PF=%.4f >= 1.00, n_trades=%d >= 30)",
                 wji_pf_val, n_trades_val)

    log.info("T3/T4 complete.")


if __name__ == "__main__":
    main()
