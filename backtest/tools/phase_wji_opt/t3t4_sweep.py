"""
T3/T4 — Stage 1 sweep for Phase WJI-OPT.

T3: WJI signal, p × hysteresis grid, 200-event train sample (cached).
T4: matched λ_V signal, same grid, same train sample.

Both use the cached Hawkes results — no Hawkes re-run.
Gate close is the sole exit (EXIT_D, LULD, watermark, re-entry all disabled).

Reads:
  results/phase_wji_poc/quality_sample_train.json    — 200-event train sample
  results/phase_wji_poc/.cache_train_results.json    — Hawkes/SF cache (t_event, mu_buy)
  config/phase_wji_opt/thresholds.json               — risk-tolerance thresholds

Writes:
  results/phase_wji_opt/stage1_wji.json
  results/phase_wji_opt/stage1_baseline.json

Escalation checks (run after T3, before T4, and after T4):
  Any swept config PF > 3.0 → hard stop (leakage ceiling)
  Configs passing all hard filters == 0 → hard stop
  Best-WJI capture_fraction <= best-λ_V capture_fraction (T4) → hard stop

Thresholds must be confirmed by Cooper before this script runs.
"""
from __future__ import annotations

import gc
import json
import logging
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
from tools.phase_wji_opt.common import (
    build_config_grid,
    wji_opt_worker,
    aggregate_config_trades,
)
from tools.phase_wji_opt.scorer import (
    compute_metrics,
    compute_per_year,
    apply_hard_filters,
    borda_rank,
    select_winner,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "results" / "phase_wji_opt"
TRAIN_SAMPLE_PATH = REPO_ROOT / "results" / "phase_wji_poc" / "quality_sample_train.json"
TRAIN_CACHE_PATH = REPO_ROOT / "results" / "phase_wji_poc" / ".cache_train_results.json"
THRESHOLDS_PATH = REPO_ROOT / "config" / "phase_wji_opt" / "thresholds.json"
MAX_WORKERS = 10
BATCH_SIZE = 50

# WJI signal params (fixed for Stage 1)
TAU_V = 180.0
BETA_SLOW = 0.01
ALPHA = 0.50


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
    import math
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    raise TypeError(f"Not serializable: {type(obj)}")


def load_sample_with_cache(
    sample_path: Path,
    cache_path: Path,
) -> list[dict]:
    """
    Load quality sample events and enrich each with t_event + mu_buy from cache.
    Returns only events found in both sample and cache with status=ok.
    """
    with open(sample_path) as f:
        sample = json.load(f)
    events = sample["events"]

    with open(cache_path) as f:
        raw_cache = json.load(f)

    # Index cache by (ticker, date)
    cache_index: dict[tuple[str, str], dict] = {}
    for r in raw_cache:
        if r.get("status") == "ok":
            cache_index[(r["ticker"], r["date"])] = r

    enriched = []
    n_missing = 0
    for e in events:
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
    log.info("Loaded %d events (sample=%d, missing=%d)", len(enriched), len(events), n_missing)
    return enriched


def run_sweep(
    events: list[dict],
    configs: list[dict],
    q_bar_cfg: dict,
    signal_type: str,
    label: str,
    alpha: float = ALPHA,
) -> list[dict]:
    """
    Run all configs over all events in parallel.
    Returns list of per-event result dicts.
    """
    config_ids = [c["config_id"] for c in configs]
    work_items = [
        {
            "ticker": e["ticker"],
            "date": e["date"],
            "mom_pct": e["mom_pct"],
            "t_event": e["t_event"],
            "mu_buy": e["mu_buy"],
            "q_bar_cfg": q_bar_cfg,
            "configs": configs,
            "signal_type": signal_type,
            "alpha": alpha,
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
        log.info(
            "%s [%s]: %.0f%% done — ok=%d skip=%d err=%d (%.1fs)",
            label, signal_type, pct, n_ok, n_skip, n_err, time.time() - t0,
        )
        gc.collect()

    log.info(
        "%s [%s]: total ok=%d skip=%d err=%d in %.1fs",
        label, signal_type, n_ok, n_skip, n_err, time.time() - t0,
    )
    return results


def build_results_dict(
    event_results: list[dict],
    configs: list[dict],
    thresholds: dict,
    split_label: str,
    signal_type: str,
) -> dict:
    """
    Aggregate all event results into per-config metric panels + selection output.
    """
    config_ids = [c["config_id"] for c in configs]
    all_trades = aggregate_config_trades(event_results, config_ids)

    metrics_by_config: dict[str, dict] = {}
    per_year_by_config: dict[str, dict] = {}
    for cid in config_ids:
        trades = all_trades[cid]
        metrics_by_config[cid] = compute_metrics(trades, thresholds)
        per_year_by_config[cid] = compute_per_year(trades)

    survivors = apply_hard_filters(config_ids, metrics_by_config, thresholds)
    ranked = borda_rank(survivors, metrics_by_config) if survivors else []
    winner = ranked[0] if ranked else None

    # Config metadata: p, hysteresis
    config_meta = {c["config_id"]: {"p": c["p"], "hysteresis": c["hysteresis"]} for c in configs}

    return {
        "meta": {
            "split": split_label,
            "signal_type": signal_type,
            "n_events": sum(1 for r in event_results if r.get("status") == "ok"),
            "thresholds": thresholds,
        },
        "configs": config_meta,
        "metrics": metrics_by_config,
        "per_year": per_year_by_config,
        "filter_results": {
            "survivors": survivors,
            "ranked": ranked,
            "winner": winner,
        },
    }


def check_pf_ceiling(metrics_by_config: dict, ceiling: float = 3.0) -> list[str]:
    """Return any config_ids where PF > ceiling."""
    return [
        cid for cid, m in metrics_by_config.items()
        if m.get("pf") is not None and m["pf"] < float("inf") and m["pf"] > ceiling
    ]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load thresholds (must exist — confirmed by Cooper before running)
    if not THRESHOLDS_PATH.exists():
        log.error(
            "Thresholds file not found: %s\n"
            "Cooper must confirm thresholds before T3 runs. Run:\n"
            "  python -m tools.phase_wji_opt.write_thresholds  (or edit manually)",
            THRESHOLDS_PATH,
        )
        sys.exit(2)
    with open(THRESHOLDS_PATH) as f:
        thresholds = json.load(f)

    if thresholds.get("_status") != "CONFIRMED":
        log.error(
            "Thresholds not confirmed. Current _status='%s'.\n"
            "Review config/phase_wji_opt/thresholds.json and set _status to 'CONFIRMED' to proceed.",
            thresholds.get("_status"),
        )
        sys.exit(2)

    log.info("Thresholds: %s", {k: v for k, v in thresholds.items() if not k.startswith("_")})

    # Load configs
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    configs = build_config_grid()
    log.info("Stage 1 grid: %d configs", len(configs))
    for c in configs:
        log.info("  %s: p=%.2f hysteresis=%s p_close=%.2f",
                 c["config_id"], c["p"], c["hysteresis"], c["p_close"])

    events = load_sample_with_cache(TRAIN_SAMPLE_PATH, TRAIN_CACHE_PATH)
    if len(events) < 50:
        log.error("Too few enriched events (%d). Aborting.", len(events))
        sys.exit(2)

    # ── T3: WJI signal sweep ─────────────────────────────────────────────
    log.info("=== T3: WJI signal sweep (%d events) ===", len(events))
    wji_results = run_sweep(events, configs, q_bar_cfg, signal_type="wji", label="T3-WJI")
    wji_output = build_results_dict(wji_results, configs, thresholds, "train", "wji")
    write_json_atomic(wji_output, OUT_DIR / "stage1_wji.json")

    # PF ceiling check (T3)
    ceiling_hits = check_pf_ceiling(wji_output["metrics"])
    if ceiling_hits:
        log.error(
            "ESCALATION: PF > 3.0 ceiling on configs %s. Hard stop — leakage/overfit.",
            ceiling_hits,
        )
        sys.exit(3)

    # Log T3 summary
    log.info("T3 WJI results:")
    for cid, m in wji_output["metrics"].items():
        passed = cid in wji_output["filter_results"]["survivors"]
        log.info(
            "  %s: capture=%.3f ev=%.3f cvar5=%.2f max_loss=%.2f n=%d pf=%.3f [%s]",
            cid,
            m.get("capture_fraction") or 0.0,
            m.get("ev") or 0.0,
            m.get("cvar5_pct") or 0.0,
            m.get("max_loss_pct") or 0.0,
            m.get("n_trades") or 0,
            m.get("pf") or 0.0,
            "PASS" if passed else "FAIL",
        )

    if not wji_output["filter_results"]["survivors"]:
        log.error("ESCALATION: 0 configs passed hard filters on T3 WJI. Hard stop.")
        sys.exit(3)

    wji_winner = wji_output["filter_results"]["winner"]
    log.info("T3 WJI winner: %s", wji_winner)

    # ── T4: λ_V signal sweep (matched baseline) ─────────────────────────
    log.info("=== T4: λ_V signal sweep (%d events) ===", len(events))
    lv_results = run_sweep(events, configs, q_bar_cfg, signal_type="lambda_v", label="T4-LV")
    lv_output = build_results_dict(lv_results, configs, thresholds, "train", "lambda_v")
    write_json_atomic(lv_output, OUT_DIR / "stage1_baseline.json")

    # PF ceiling check (T4)
    ceiling_hits_lv = check_pf_ceiling(lv_output["metrics"])
    if ceiling_hits_lv:
        log.error(
            "ESCALATION: PF > 3.0 ceiling on λ_V configs %s. Hard stop.",
            ceiling_hits_lv,
        )
        sys.exit(3)

    lv_winner = lv_output["filter_results"]["winner"]
    log.info("T4 λ_V winner: %s", lv_winner)

    # T4a escalation: best WJI capture_fraction <= best λ_V capture_fraction
    wji_best_cf = wji_output["metrics"].get(wji_winner, {}).get("capture_fraction") if wji_winner else None
    lv_best_cf = lv_output["metrics"].get(lv_winner, {}).get("capture_fraction") if lv_winner else None

    if wji_best_cf is not None and lv_best_cf is not None:
        log.info(
            "T4a capture_fraction check: WJI=%s %.4f vs λ_V=%s %.4f",
            wji_winner, wji_best_cf, lv_winner, lv_best_cf,
        )
        if wji_best_cf <= lv_best_cf:
            log.error(
                "ESCALATION T4a: best WJI capture_fraction (%.4f) <= best λ_V (%.4f). "
                "WJI signal adds nothing. Hard stop.",
                wji_best_cf, lv_best_cf,
            )
            sys.exit(3)
    else:
        log.warning(
            "T4a check incomplete: WJI winner=%s (cf=%s), λ_V winner=%s (cf=%s)",
            wji_winner, wji_best_cf, lv_winner, lv_best_cf,
        )

    log.info("T3/T4 sweep complete. Stage 2 (alpha sweep) requires explicit approval.")


if __name__ == "__main__":
    main()
