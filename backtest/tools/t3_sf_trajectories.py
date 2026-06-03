"""
T3 — Precompute setup filter trajectories for all training and val events.

Runs the setup filter (signal computation only) on all 300 training events
and all 100 val events. Stores Q_tilde trajectories for use in T4/T5 reporting.

Escalation: if > 50% of training events never qualify, hard stop.

Writes:
  results/phase_epg_opt2_sf/sf_summary_train.json   — per-event summary
  results/phase_epg_opt2_sf/sf_summary_val.json
  results/phase_epg_opt2_sf/sf_leg_class_stats.json  — first_qualify_bar by leg class
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from data.loaders.trades import load_trades, _session_ns_bounds, list_events
from data.schemas.mom_db import CONFIG_DIR
from core.filters.setup_filter import (
    run_setup_filter, compute_lookback_low, PSI_LOOKBACK_DAYS,
)
from tools.sweep_runner_opt2 import precompute_sf_trajectory

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

TRAIN_SAMPLE_PATH = REPO_ROOT / "results" / "phase_epg_grt" / "train_sample.json"
OPT2_VAL_COMPARISON = REPO_ROOT / "results" / "phase_epg_opt2" / "val_seed99" / "comparison.json"
LEG_CLASS_PATH = REPO_ROOT / "results" / "phase_epg_opt2" / "leg_classification.json"
OUT_DIR = REPO_ROOT / "results" / "phase_epg_opt2_sf"
HOLDOUT_PATH = CONFIG_DIR / "holdout_boundary.json"


def compute_sf_summary_for_event(ev: dict, compute_psi: bool = True) -> dict:
    """Compute SF summary for one event. Returns per-event stats dict."""
    ticker, date = ev["ticker"], ev["date"]
    try:
        td = load_trades(ticker, date, ev.get("mom_pct", 0.3))
        start_ns, end_ns = _session_ns_bounds(date)

        # ψ check
        psi_passes = True
        if compute_psi:
            try:
                lookback_low = compute_lookback_low(td.timestamps, td.prices, start_ns)
                event_close = float(td.prices[-1]) if td.n_trades > 0 else 0.0
                from core.filters.setup_filter import check_psi
                psi_passes = check_psi(event_close, lookback_low) if lookback_low > 0 else False
            except Exception:
                psi_passes = False

        sf = precompute_sf_trajectory(td, start_ns, end_ns)

        return {
            "ticker": ticker,
            "date": date,
            "n_bars": sf.n_bars,
            "first_qualify_bar": sf.first_qualify_bar,
            "mean_q_tilde": round(sf.mean_q_tilde, 4),
            "psi_passes": psi_passes,
            "never_qualifies": sf.first_qualify_bar == -1,
            "min_q_tilde": round(float(np.min(sf.q_tilde)) if sf.n_bars > 0 else 0.0, 4),
        }
    except Exception as e:
        return {
            "ticker": ticker, "date": date,
            "n_bars": 0, "first_qualify_bar": -1,
            "mean_q_tilde": 0.0, "psi_passes": False,
            "never_qualifies": True, "min_q_tilde": 0.0,
            "error": str(e)[:200],
        }


def summarize_and_write(events: list[dict], out_path: Path, label: str) -> list[dict]:
    summaries = []
    n_never_qualify = 0
    n_psi_fail = 0
    first_qualify_bars = []

    for i, ev in enumerate(events):
        s = compute_sf_summary_for_event(ev)
        summaries.append(s)
        if s["never_qualifies"]:
            n_never_qualify += 1
        if not s["psi_passes"]:
            n_psi_fail += 1
        if s["first_qualify_bar"] >= 0:
            first_qualify_bars.append(s["first_qualify_bar"])
        if (i + 1) % 50 == 0 or (i + 1) == len(events):
            log.info("  %s: %d/%d processed", label, i + 1, len(events))

    pct_never = n_never_qualify / len(events) * 100 if events else 0
    mean_fqb = sum(first_qualify_bars) / len(first_qualify_bars) if first_qualify_bars else -1

    log.info("%s: %d events total, %d never qualify (%.1f%%), %d psi fail, mean first_qualify_bar=%.1f",
             label, len(events), n_never_qualify, pct_never, n_psi_fail, mean_fqb)

    with open(out_path, "w") as f:
        json.dump({
            "n_events": len(events),
            "n_never_qualify": n_never_qualify,
            "pct_never_qualify": round(pct_never, 1),
            "n_psi_fail": n_psi_fail,
            "mean_first_qualify_bar_minutes": round(mean_fqb, 1),
            "events": summaries,
        }, f, indent=2)
    log.info("Written: %s", out_path)
    return summaries


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Training events
    with open(TRAIN_SAMPLE_PATH) as f:
        train_data = json.load(f)
    train_events = train_data["events"]
    log.info("Training events: %d", len(train_events))

    train_summaries = summarize_and_write(
        train_events,
        OUT_DIR / "sf_summary_train.json",
        "train",
    )

    # Escalation: > 50% never qualify
    n_never = sum(1 for s in train_summaries if s["never_qualifies"])
    pct_never = n_never / len(train_events) * 100
    if pct_never > 50:
        log.error("T3 ESCALATION: %.1f%% of training events never qualify (>50%%). "
                  "Post sf_summary_train.json and await instruction.", pct_never)
        sys.exit(2)

    # Val events (load the same 100-event val sample from comparison.json)
    # The val events were sampled in T8; re-derive from full val list with same seed
    from data.loaders.trades import list_events as _list_events
    import numpy as _np
    from tools.sweep_runner_opt2 import EPG_WARMUP

    with open(HOLDOUT_PATH) as f:
        boundary = json.load(f)
    val_start = boundary["val_split_start_date"]
    test_start = boundary["test_split_start_date"]
    all_events = _list_events()
    val_all = [e for e in all_events if val_start <= e["date"] < test_start]

    # Replicate T8's stratified sample (seed=99, n=100)
    from tools.t8_val_validate_opt2 import stratified_sample
    val_sample = stratified_sample(val_all, 100, 99)
    log.info("Val events: %d", len(val_sample))

    summarize_and_write(
        val_sample,
        OUT_DIR / "sf_summary_val.json",
        "val",
    )

    # Leg class cross-reference
    if LEG_CLASS_PATH.exists():
        with open(LEG_CLASS_PATH) as f:
            leg_data = json.load(f)
        classifications = {(c["ticker"], c["date"]): c["leg_class"]
                           for c in leg_data.get("classifications", [])}
        by_class: dict[str, list[int]] = {}
        for s in train_summaries:
            key = (s["ticker"], s["date"])
            lc = classifications.get(key, "unknown")
            if s["first_qualify_bar"] >= 0:
                by_class.setdefault(lc, []).append(s["first_qualify_bar"])

        class_stats = {}
        for lc, fqbs in sorted(by_class.items()):
            class_stats[lc] = {
                "n_qualify": len(fqbs),
                "mean_first_qualify_bar_minutes": round(sum(fqbs) / len(fqbs), 1),
                "min_first_qualify_bar_minutes": min(fqbs),
                "max_first_qualify_bar_minutes": max(fqbs),
            }
            log.info("  leg_class=%s: mean_first_qualify=%.1f min, n=%d",
                     lc, class_stats[lc]["mean_first_qualify_bar_minutes"],
                     class_stats[lc]["n_qualify"])

        with open(OUT_DIR / "sf_leg_class_stats.json", "w") as f:
            json.dump(class_stats, f, indent=2)
        log.info("Written: %s", OUT_DIR / "sf_leg_class_stats.json")

    log.info("\nT3 complete.")


if __name__ == "__main__":
    main()
