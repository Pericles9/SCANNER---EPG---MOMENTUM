"""
T3 — Phase WJI-SlowEMA 25-config sweep on 100-event val sample (seed=42).

Grid: tau_slow ∈ {300,600,900,1200,1800} × p_open ∈ {0.70,0.75,0.80,0.85,0.90}
      p_close = 0.55 (fixed)

Reads:
  results/phase_wji_poc/quality_sample_val.json   — 100-event val sample
  results/phase_wji_poc/.cache_val_results.json   — Hawkes cache (t_event, mu_buy)
  config/q_bar_tiers.json

Writes:
  results/phase_wji_slow_ema/t3_sweep.json

T1e note
--------
WJI-OPT baseline (PF=1.1881) was computed WITHOUT halt-adjusted time.
This phase uses halt-adjusted dt — results are directionally comparable
but n_trades may differ slightly from the baseline on halted events.

Escalation (hard stop)
-----------------------
T3b: If no config achieves CVaR5 ≥ -10.0%, post results to chat, flag FAIL, stop.
T3c: If median PASS/FAIL cycle count > 8/event, flag stagnation warning.
"""
from __future__ import annotations

import gc
import json
import logging
import math
import os
import statistics
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from data.schemas.mom_db import CONFIG_DIR
from tools.phase_wji_slow_ema.common import (
    build_config_grid,
    wji_slow_ema_worker,
    aggregate_config_trades,
    TAU_V, BETA_SLOW, ALPHA,
)
from tools.phase_wji_opt.scorer import compute_metrics

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

OUT_DIR = REPO_ROOT / "results" / "phase_wji_slow_ema"
VAL_SAMPLE_PATH = REPO_ROOT / "results" / "phase_wji_poc" / "quality_sample_val.json"
VAL_CACHE_PATH = REPO_ROOT / "results" / "phase_wji_poc" / ".cache_val_results.json"

MAX_WORKERS = 8
BATCH_SIZE = 25

# Baseline from WJI-OPT p065_single on 100-event val seed=42
BASELINE = {
    "config_id": "p065_single",
    "pf": 1.1881,
    "cvar5_pct": -9.16,
    "n_trades": 2134,
    "note": "WJI-OPT computed WITHOUT halt-adjusted time — compare PF directionally",
}

# Selection thresholds
CVAR5_MIN = -10.0    # T3b hard stop if ALL configs fail this
CYCLE_WARN = 8       # T3c stagnation flag if median > this


def _json_default(obj):
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    raise TypeError(f"Not serializable: {type(obj)}")


def write_json_atomic(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
    ) as f:
        json.dump(data, f, indent=2, default=_json_default)
        tmp = Path(f.name)
    os.replace(str(tmp), str(path))
    log.info("Written: %s", path)


def load_val_events() -> list[dict]:
    """Load 100-event val sample enriched with t_event and mu_buy from cache."""
    with open(VAL_SAMPLE_PATH) as f:
        sample = json.load(f)
    events = sample["events"]

    with open(VAL_CACHE_PATH) as f:
        raw_cache = json.load(f)

    cache_index = {
        (r["ticker"], r["date"]): r
        for r in raw_cache
        if r.get("status") == "ok"
    }

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

    if n_missing:
        log.warning("Missing from cache: %d events", n_missing)
    log.info("Val sample: %d events (missing=%d)", len(enriched), n_missing)
    return enriched


def run_sweep(events: list[dict], configs: list[dict], q_bar_cfg: dict) -> list[dict]:
    """Run all 25 configs over all events in parallel batches."""
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
            "tau_v": TAU_V,
            "beta_slow": BETA_SLOW,
            "alpha": ALPHA,
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
            futs = {executor.submit(wji_slow_ema_worker, item): item for item in batch}
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
                    log.warning("Error: %s/%s — %s", r.get("ticker"), r.get("date"), r.get("error", ""))
        pct = (batch_start + len(batch)) / len(work_items) * 100
        log.info("%.0f%% done — ok=%d skip=%d err=%d (%.1fs)", pct, n_ok, n_skip, n_err, time.time() - t0)
        gc.collect()

    log.info("Sweep done: ok=%d skip=%d err=%d in %.1fs", n_ok, n_skip, n_err, time.time() - t0)
    return results


def _compute_pf(pnl_list: list[float]) -> float:
    wins = sum(p for p in pnl_list if p > 0)
    losses = sum(abs(p) for p in pnl_list if p < 0)
    if losses == 0:
        return float("inf") if wins > 0 else float("nan")
    return wins / losses


def _compute_cvar5(pnl_list: list[float]) -> float:
    n = len(pnl_list)
    if n == 0:
        return float("nan")
    k = max(1, math.floor(0.05 * n))
    tail = sorted(pnl_list)[:k]
    return sum(tail) / len(tail)


def build_results(
    event_results: list[dict],
    configs: list[dict],
) -> dict:
    """Aggregate per-event results into per-config metric table + selection."""
    config_ids = [c["config_id"] for c in configs]
    agg = aggregate_config_trades(event_results, config_ids)

    rows: list[dict] = []
    for cfg in configs:
        cid = cfg["config_id"]
        trades = agg[cid]["trades"]
        cycle_counts = agg[cid]["cycle_counts"]

        pnl_list = [t["pnl_pct"] for t in trades]
        pf = _compute_pf(pnl_list)
        cvar5 = _compute_cvar5(pnl_list)
        ev = sum(pnl_list) / len(pnl_list) if pnl_list else float("nan")
        cycle_median = statistics.median(cycle_counts) if cycle_counts else 0.0

        rows.append({
            "config_id": cid,
            "tau_slow": cfg["tau_slow"],
            "p_open": cfg["p_open"],
            "p_close": cfg["p_close"],
            "n_trades": len(trades),
            "pf": pf,
            "cvar5_pct": cvar5,
            "ev_pct": ev,
            "cycle_count_median": cycle_median,
            "meets_cvar5_threshold": (not math.isnan(cvar5)) and cvar5 >= CVAR5_MIN,
            "meets_baseline_pf": (not math.isnan(pf)) and pf >= BASELINE["pf"],
        })

    # Sort by PF descending for display
    rows.sort(key=lambda r: r["pf"] if not math.isnan(r["pf"]) else -1, reverse=True)

    # Select winner: highest PF among CVaR5 ≥ threshold; ties → larger tau_slow
    survivors = [r for r in rows if r["meets_cvar5_threshold"] and not math.isnan(r["pf"])]
    if survivors:
        survivors.sort(key=lambda r: (r["pf"], r["tau_slow"]), reverse=True)
        winner = survivors[0]
    else:
        winner = None

    # Stagnation check (T3c)
    all_cycle_medians = [r["cycle_count_median"] for r in rows]
    overall_cycle_median = statistics.median(all_cycle_medians) if all_cycle_medians else 0.0
    stagnation_flag = overall_cycle_median > CYCLE_WARN

    return {
        "baseline": BASELINE,
        "t1e_note": (
            "WJI-OPT baseline was computed WITHOUT halt-adjusted time. "
            "This phase uses halt-adjusted dt throughout (T1b). "
            "PF comparisons are valid; n_trades may differ on halted events."
        ),
        "meta": {
            "n_events_ok": sum(1 for r in event_results if r.get("status") == "ok"),
            "n_events_skipped": sum(1 for r in event_results if r.get("status") == "skipped"),
            "n_events_error": sum(1 for r in event_results if r.get("status") == "error"),
            "n_events_with_halts": sum(1 for r in event_results if r.get("status") == "ok" and r.get("n_halts", 0) > 0),
            "tau_v": TAU_V,
            "beta_slow": BETA_SLOW,
            "alpha": ALPHA,
            "p_close_fixed": 0.55,
            "cvar5_threshold": CVAR5_MIN,
            "cycle_warn_threshold": CYCLE_WARN,
        },
        "configs": rows,
        "winner": winner,
        "stagnation": {
            "overall_cycle_median": overall_cycle_median,
            "flagged": stagnation_flag,
        },
        "n_survivors_cvar5": len(survivors),
        "n_survivors_pf_and_cvar5": len([r for r in survivors if r["meets_baseline_pf"]]),
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    configs = build_config_grid()
    log.info("Config grid: %d configs", len(configs))
    for c in configs:
        log.info("  %s: tau_slow=%.0f p_open=%.2f p_close=%.2f",
                 c["config_id"], c["tau_slow"], c["p_open"], c["p_close"])

    events = load_val_events()
    if len(events) < 50:
        log.error("Too few events (%d). Aborting.", len(events))
        sys.exit(2)

    log.info("=== T3 WJI-SlowEMA sweep: %d events × %d configs ===", len(events), len(configs))
    event_results = run_sweep(events, configs, q_bar_cfg)

    output = build_results(event_results, configs)
    write_json_atomic(output, OUT_DIR / "t3_sweep.json")

    # ── Escalation checks ────────────────────────────────────────────────
    log.info("\n=== T3 Results ===")
    log.info("%-20s %8s %10s %10s %10s %8s", "config_id", "tau_slow", "p_open", "pf", "cvar5_pct", "n_trades")
    log.info("-" * 75)
    for row in output["configs"]:
        flag = ""
        if row["meets_cvar5_threshold"] and row["meets_baseline_pf"]:
            flag = " ✓ (beats baseline + CVaR5)"
        elif row["meets_cvar5_threshold"]:
            flag = " ~ (CVaR5 OK, PF below baseline)"
        pf_str = f"{row['pf']:.4f}" if not math.isnan(row["pf"]) else "nan"
        log.info("%-20s %8.0f %10.2f %10s %10.2f %8d%s",
                 row["config_id"], row["tau_slow"], row["p_open"],
                 pf_str, row["cvar5_pct"], row["n_trades"], flag)

    # T3b hard stop
    if output["n_survivors_cvar5"] == 0:
        log.error(
            "\nESCALATION T3b: NO config achieves CVaR5 ≥ %.1f%%. "
            "Hard stop — post results to chat and flag FAIL. "
            "Do NOT proceed to T4/T5.",
            CVAR5_MIN,
        )
        sys.exit(3)

    # T3c stagnation warning
    if output["stagnation"]["flagged"]:
        log.warning(
            "T3c STAGNATION WARNING: median PASS/FAIL cycle count = %.1f > threshold %d. "
            "Gate is cycling excessively — consider larger tau_slow or p_open.",
            output["stagnation"]["overall_cycle_median"], CYCLE_WARN,
        )
    else:
        log.info(
            "T3c cycle check: median=%.1f (threshold=%d) — OK",
            output["stagnation"]["overall_cycle_median"], CYCLE_WARN,
        )

    winner = output["winner"]
    if winner:
        log.info(
            "\nWinner: %s (tau_slow=%.0f p_open=%.2f) — PF=%.4f CVaR5=%.2f%% n=%d",
            winner["config_id"], winner["tau_slow"], winner["p_open"],
            winner["pf"], winner["cvar5_pct"], winner["n_trades"],
        )
        if winner["meets_baseline_pf"]:
            log.info("Winner BEATS WJI-OPT baseline (PF=%.4f). Proceed to T4/T5/T6.", BASELINE["pf"])
        else:
            log.info(
                "Winner PF=%.4f does NOT beat baseline PF=%.4f. "
                "Report to Cooper before proceeding.",
                winner["pf"], BASELINE["pf"],
            )
    else:
        log.error("No winner selected (no survivors meet CVaR5 threshold).")

    log.info("\nT3 sweep complete. Results written to %s", OUT_DIR / "t3_sweep.json")
    log.info("Next steps: T4 diagnostic charts, T5 trade charts, T6 docs.")
    log.info("⛔ Await Cooper approval before T7 (full val run).")


if __name__ == "__main__":
    main()
