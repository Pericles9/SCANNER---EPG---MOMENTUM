"""
T6 — Val confirmation for Phase WJI-OPT.

Runs the winning WJI config (from T3-RESCORE) and the best λ_V config (from T4)
on the 100-event val cache.

Escalation checks (T6b):
  1. Selected config val cvar5_pct < -8.0
  2. Selected config val n_trades < 60
  3. Val capture_fraction < 0.80 × train capture_fraction
  4. Any single year's val cvar5_pct < -16.0

Reads:
  results/phase_wji_poc/quality_sample_val.json
  results/phase_wji_poc/.cache_val_results.json
  results/phase_wji_opt/stage1_wji_rescored.json
  results/phase_wji_opt/stage1_baseline.json

Writes:
  results/phase_wji_opt/val_selected.json   — winning WJI config on val
  results/phase_wji_opt/val_baseline.json   — matching λ_V config on val
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
from tools.phase_wji_opt.common import wji_opt_worker, aggregate_config_trades
from tools.phase_wji_opt.scorer import compute_metrics, compute_per_year

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

VAL_SAMPLE_PATH = REPO_ROOT / "results" / "phase_wji_poc" / "quality_sample_val.json"
VAL_CACHE_PATH = REPO_ROOT / "results" / "phase_wji_poc" / ".cache_val_results.json"
RESCORED_PATH = REPO_ROOT / "results" / "phase_wji_opt" / "stage1_wji_rescored.json"
BASELINE_PATH = REPO_ROOT / "results" / "phase_wji_opt" / "stage1_baseline.json"
OUT_SELECTED = REPO_ROOT / "results" / "phase_wji_opt" / "val_selected.json"
OUT_BASELINE = REPO_ROOT / "results" / "phase_wji_opt" / "val_baseline.json"
MAX_WORKERS = 10
BATCH_SIZE = 50
TAU_V = 180.0
BETA_SLOW = 0.01
ALPHA = 0.50

VAL_THRESHOLDS = {
    "n_trades_floor": 60,
    "cvar5_floor_pct": -8.0,
    "pf_floor": 1.0,
}
T4_FINDING = (
    "WJI passes CVaR5 floor; λ_V does not. WJI trades ~28% lower capture_fraction for "
    "tail-quality improvement. WJI is the sole viable signal in a gate-close-only design. "
    "Proper WJI-vs-λ_V+stop comparison deferred to stop-loss phase."
)
CVAR5_FLOOR = -8.0
N_TRADES_FLOOR_VAL = 60
CAPTURE_FRACTION_DECAY = 0.80
CVAR5_PER_YEAR_FLOOR = -16.0


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


def load_val_sample_with_cache() -> list[dict]:
    with open(VAL_SAMPLE_PATH) as f:
        sample = json.load(f)
    with open(VAL_CACHE_PATH) as f:
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
        log.warning("%d val events not in cache (skipped)", n_missing)
    log.info("Val sample loaded: %d events (sample=%d, missing=%d)",
             len(enriched), len(sample["events"]), n_missing)
    return enriched


def run_single_config_sweep(
    events: list[dict],
    cfg: dict,
    q_bar_cfg: dict,
    signal_type: str,
    label: str,
) -> list[dict]:
    """Run one config over all events in parallel. Returns per-event results."""
    work_items = [
        {
            "ticker": e["ticker"],
            "date": e["date"],
            "mom_pct": e["mom_pct"],
            "t_event": e["t_event"],
            "mu_buy": e["mu_buy"],
            "q_bar_cfg": q_bar_cfg,
            "configs": [cfg],
            "signal_type": signal_type,
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


def build_output(
    event_results: list[dict],
    cfg: dict,
    train_capture_fraction: float,
    split: str,
    signal_type: str,
) -> dict:
    cid = cfg["config_id"]
    all_trades = aggregate_config_trades(event_results, [cid])
    trades = all_trades[cid]

    metrics = compute_metrics(trades, VAL_THRESHOLDS)
    per_year = compute_per_year(trades)

    # Per-event summary for T7
    events_with_trades: list[dict] = []
    for r in event_results:
        if r.get("status") != "ok":
            continue
        ev_trades = r.get("config_results", {}).get(cid, [])
        if not ev_trades:
            continue
        ev_pnl = [t["pnl_pct"] for t in ev_trades]
        ev_avail = [t["available_move_pct"] for t in ev_trades]
        ev_sum_avail = sum(ev_avail)
        ev_cf = sum(ev_pnl) / ev_sum_avail if ev_sum_avail > 0 else None
        wins = sum(p for p in ev_pnl if p > 0)
        losses = sum(abs(p) for p in ev_pnl if p < 0)
        ev_pf = wins / losses if losses > 0 else (float("inf") if wins > 0 else float("nan"))
        events_with_trades.append({
            "ticker": r["ticker"],
            "date": r["date"],
            "n_trades": len(ev_trades),
            "event_pnl_pct": sum(ev_pnl),
            "event_pf": None if (math.isinf(ev_pf) or math.isnan(ev_pf)) else ev_pf,
            "event_capture_fraction": ev_cf,
            "max_loss_pct": min(ev_pnl),
        })

    return {
        "meta": {
            "split": split,
            "signal_type": signal_type,
            "config_id": cid,
            "p": cfg["p"],
            "hysteresis": cfg["hysteresis"],
            "p_close": cfg["p_close"],
            "n_events_run": sum(1 for r in event_results if r.get("status") == "ok"),
            "n_events_with_trades": len(events_with_trades),
            "val_thresholds": VAL_THRESHOLDS,
            "train_capture_fraction": train_capture_fraction,
        },
        "metrics": {
            cid: {
                "capture_fraction": metrics.get("capture_fraction"),
                "ev": metrics.get("ev"),
                "cvar5_pct": metrics.get("cvar5_pct"),
                "max_loss_pct": metrics.get("max_loss_pct"),
                "worst_event": metrics.get("worst_event"),
                "median_pct": metrics.get("median_pct"),
                "n_trades": metrics.get("n_trades"),
                "pf": metrics.get("pf"),
                "n_cvar5_trades": metrics.get("n_cvar5_trades"),
            }
        },
        "per_year": {cid: per_year},
        "events_with_trades": events_with_trades,
    }


def check_escalations(
    metrics: dict,
    per_year: dict,
    train_capture_fraction: float,
    config_id: str,
) -> None:
    """Run T6b escalation checks. Exits with code 3 on first trigger."""
    cf = metrics.get("capture_fraction")
    n = metrics.get("n_trades", 0)
    cvar5 = metrics.get("cvar5_pct")
    max_loss = metrics.get("max_loss_pct")

    log.info("T6b escalation checks for %s:", config_id)
    log.info("  n_trades: %d (floor %d)", n, N_TRADES_FLOOR_VAL)
    log.info("  cvar5_pct: %s (floor %.1f)", f"{cvar5:.2f}" if cvar5 is not None else "None", CVAR5_FLOOR)
    log.info("  capture_fraction: %s (train %.4f, 0.80×train %.4f)",
             f"{cf:.4f}" if cf is not None else "None",
             train_capture_fraction, CAPTURE_FRACTION_DECAY * train_capture_fraction)

    # 1. cvar5
    if cvar5 is None or cvar5 < CVAR5_FLOOR:
        log.error(
            "ESCALATION T6b-1: val cvar5_pct=%.2f < floor %.1f. Hard stop.",
            cvar5 if cvar5 is not None else float("nan"), CVAR5_FLOOR,
        )
        sys.exit(3)

    # 2. n_trades
    if n < N_TRADES_FLOOR_VAL:
        log.error(
            "ESCALATION T6b-2: val n_trades=%d < floor %d. Hard stop.",
            n, N_TRADES_FLOOR_VAL,
        )
        sys.exit(3)

    # 3. capture_fraction decay
    if cf is not None and cf < CAPTURE_FRACTION_DECAY * train_capture_fraction:
        log.error(
            "ESCALATION T6b-3: val capture_fraction=%.4f < 0.80 × train %.4f. Hard stop.",
            cf, train_capture_fraction,
        )
        sys.exit(3)

    # 4. Per-year cvar5
    for yr, yr_m in per_year.items():
        yr_cvar5 = yr_m.get("cvar5_pct")
        if yr_cvar5 is not None and yr_cvar5 < CVAR5_PER_YEAR_FLOOR:
            log.error(
                "ESCALATION T6b-4: year %s val cvar5_pct=%.2f < 2×floor %.1f. Hard stop.",
                yr, yr_cvar5, CVAR5_PER_YEAR_FLOOR,
            )
            sys.exit(3)

    log.info("T6b PASS: all escalation checks cleared.")


def main() -> None:
    for p in [RESCORED_PATH, BASELINE_PATH]:
        if not p.exists():
            log.error("Required file not found: %s", p)
            sys.exit(1)

    with open(RESCORED_PATH) as f:
        rescored = json.load(f)
    wji_winner_id = rescored["filter_results"].get("winner")
    if not wji_winner_id:
        log.error("No WJI winner in T3-RESCORE. Aborting.")
        sys.exit(1)

    with open(BASELINE_PATH) as f:
        baseline = json.load(f)
    lv_winner_id = baseline["filter_results"].get("winner")
    lv_winner_is_fallback = False
    if not lv_winner_id:
        from tools.phase_wji_opt.scorer import borda_rank
        single_ids = [cid for cid in baseline["configs"] if "single" in cid]
        raw_metrics = {cid: baseline["configs"][cid] for cid in single_ids}
        raw_ranked = borda_rank(single_ids, raw_metrics)
        lv_winner_id = raw_ranked[0] if raw_ranked else None
        if lv_winner_id:
            lv_winner_is_fallback = True
            log.warning(
                "No λ_V survivor from T4. Using best-raw single-mode config: %s (failed hard filters)",
                lv_winner_id,
            )
        else:
            log.error("No single-mode λ_V configs found. Aborting.")
            sys.exit(1)

    t4_finding = rescored["meta"].get("t4_finding", T4_FINDING)

    log.info("WJI winner: %s", wji_winner_id)
    log.info("λ_V config (baseline): %s%s", lv_winner_id, " [fallback — failed T4 hard filters]" if lv_winner_is_fallback else "")

    from tools.phase_wji_opt.common import build_config_grid
    grid = build_config_grid()
    grid_by_id = {c["config_id"]: c for c in grid}

    wji_cfg = grid_by_id[wji_winner_id]
    lv_cfg = grid_by_id[lv_winner_id]

    # Train capture fractions for T6b-3 check
    wji_train_cf = rescored["configs"][wji_winner_id].get("capture_fraction") or 0.0
    lv_train_cf = baseline["configs"][lv_winner_id].get("capture_fraction") or 0.0

    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    events = load_val_sample_with_cache()
    if len(events) < 20:
        log.error("Too few val events (%d). Aborting.", len(events))
        sys.exit(2)

    # Run WJI winner on val
    log.info("=== T6: WJI config %s on val (%d events) ===", wji_winner_id, len(events))
    wji_results = run_single_config_sweep(events, wji_cfg, q_bar_cfg, "wji", "T6-WJI")
    wji_output = build_output(wji_results, wji_cfg, wji_train_cf, "val", "wji")
    wji_output["meta"]["t4_finding"] = t4_finding
    wji_metrics = wji_output["metrics"][wji_winner_id]
    wji_per_year = wji_output["per_year"][wji_winner_id]
    write_json_atomic(wji_output, OUT_SELECTED)

    # Log val metrics
    log.info("WJI val metrics: n=%d cf=%.4f ev=%.3f cvar5=%.2f max_loss=%.2f pf=%.3f",
             wji_metrics.get("n_trades", 0),
             wji_metrics.get("capture_fraction") or 0.0,
             wji_metrics.get("ev") or 0.0,
             wji_metrics.get("cvar5_pct") or 0.0,
             wji_metrics.get("max_loss_pct") or 0.0,
             wji_metrics.get("pf") or 0.0)

    # T6b escalation checks on WJI
    check_escalations(wji_metrics, wji_per_year, wji_train_cf, wji_winner_id)

    # Run λ_V winner on val
    log.info("=== T6: λ_V config %s on val (%d events) ===", lv_winner_id, len(events))
    lv_results = run_single_config_sweep(events, lv_cfg, q_bar_cfg, "lambda_v", "T6-LV")
    lv_output = build_output(lv_results, lv_cfg, lv_train_cf, "val", "lambda_v")
    lv_output["meta"]["t4_finding"] = t4_finding
    lv_output["meta"]["lv_winner_is_fallback"] = lv_winner_is_fallback
    write_json_atomic(lv_output, OUT_BASELINE)

    lv_metrics = lv_output["metrics"][lv_winner_id]
    log.info("λ_V val metrics: n=%d cf=%.4f ev=%.3f cvar5=%.2f max_loss=%.2f pf=%.3f",
             lv_metrics.get("n_trades", 0),
             lv_metrics.get("capture_fraction") or 0.0,
             lv_metrics.get("ev") or 0.0,
             lv_metrics.get("cvar5_pct") or 0.0,
             lv_metrics.get("max_loss_pct") or 0.0,
             lv_metrics.get("pf") or 0.0)

    log.info("T6 complete. Proceed to T7 (chart generation).")


if __name__ == "__main__":
    main()
