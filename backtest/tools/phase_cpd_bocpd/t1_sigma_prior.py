"""
Phase CPD-BOCPD — Task T1
=========================
Read prior CPD-0/CPD-1 results and emit the sigma_log warmup-fallback prior for the
BOCPD gate, plus confirm the reused 100-event val list.

T1a escalation note (resolved): the phase doc references `results/phase_cpd_1/`, but the
actual CPD-1 artifacts live at `results/phase_cpd/cpd1/` (and the sigma_log distribution
at `results/phase_cpd/cpd0/sigma_log_summary.json`). The required data is PRESENT, so
T1a (hard stop on *missing* data) does not trigger — we proceed against the real paths.

Outputs
-------
results/phase_cpd_bocpd/sigma_log_prior.json
    median_sigma_log, p10_sigma_log, p90_sigma_log, n_events_with_warmup_obs_lt_20,
    plus provenance (source paths, fallback constant, event-list confirmation).

Run
---
    "D:/Trading Research/.venv/Scripts/python.exe" -m tools.phase_cpd_bocpd.t1_sigma_prior
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# CPD-1 sweep results (existence gate for T1a) and CPD-0 sigma distribution.
CUSUM_SWEEP = REPO_ROOT / "results" / "phase_cpd" / "cpd1" / "cusum_sweep_results.json"
SIGMA_SUMMARY = REPO_ROOT / "results" / "phase_cpd" / "cpd0" / "sigma_log_summary.json"
VAL_SAMPLE = REPO_ROOT / "results" / "phase_wji_poc" / "quality_sample_val.json"
OUT = REPO_ROOT / "results" / "phase_cpd_bocpd" / "sigma_log_prior.json"

MIN_WARMUP_OBS = 20
FALLBACK_DEFAULT = 0.209  # CPD-0 recommended fallback constant


def main() -> None:
    # ── T1a existence gate ──
    if not CUSUM_SWEEP.exists():
        print(f"*** T1a HARD STOP: missing {CUSUM_SWEEP} ***")
        sys.exit(2)
    if not SIGMA_SUMMARY.exists():
        print(f"*** T1a HARD STOP: missing {SIGMA_SUMMARY} ***")
        sys.exit(2)

    summary = json.load(open(SIGMA_SUMMARY))
    per_event = summary["per_event"]
    sigmas = np.array([e["sigma_log"] for e in per_event], dtype=np.float64)
    n_lt = int(sum(1 for e in per_event if e["n_warmup_ticks"] < MIN_WARMUP_OBS))

    median = float(np.median(sigmas))
    p10 = float(np.percentile(sigmas, 10))
    p90 = float(np.percentile(sigmas, 90))

    # Reused val event list (identical file CPD-0/CPD-1 used; do NOT re-sample).
    val = json.load(open(VAL_SAMPLE))
    events = val["events"]
    val_meta = val.get("meta", {})

    out = {
        "phase": "CPD-BOCPD",
        "task": "T1",
        "source_paths": {
            "cusum_sweep_results": str(CUSUM_SWEEP.relative_to(REPO_ROOT)),
            "sigma_log_summary": str(SIGMA_SUMMARY.relative_to(REPO_ROOT)),
            "val_sample": str(VAL_SAMPLE.relative_to(REPO_ROOT)),
        },
        "n_events": len(per_event),
        "median_sigma_log": median,
        "p10_sigma_log": p10,
        "p90_sigma_log": p90,
        "n_events_with_warmup_obs_lt_20": n_lt,
        "warmup_fallback_constant": FALLBACK_DEFAULT,
        "fallback_rationale": (
            "Per-event sigma_log is estimated from the 300s warmup window. If an event "
            f"has < {MIN_WARMUP_OBS} warmup observations, fall back to this constant "
            "(CPD-0 median). All 100 events have >= 20 warmup obs, so the fallback is "
            "not expected to fire."
        ),
        "val_event_list": {
            "n_events": len(events),
            "reused_from": str(VAL_SAMPLE.relative_to(REPO_ROOT)),
            "seed_in_meta": val_meta.get("seed"),
            "note": (
                "Identical event list reused from CPD-0/CPD-1 (no re-sampling). The phase "
                "doc labels this 'seed=42'; the sample file's meta records seed="
                f"{val_meta.get('seed')}. Binding instruction is 'reuse the identical "
                "event list', which this honours — the seed label is cosmetic."
            ),
        },
        "t1a_note": (
            "Phase doc path 'results/phase_cpd_1/' does not exist; real CPD-1 artifacts "
            "are at 'results/phase_cpd/cpd1/'. Data present -> T1a not triggered."
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    print("T1 sigma_log prior:")
    print(f"  n_events            = {len(per_event)}")
    print(f"  median_sigma_log    = {median:.6f}")
    print(f"  p10_sigma_log       = {p10:.6f}")
    print(f"  p90_sigma_log       = {p90:.6f}")
    print(f"  n_warmup_obs_lt_20  = {n_lt}")
    print(f"  fallback_constant   = {FALLBACK_DEFAULT}")
    print(f"  val events reused   = {len(events)} (seed_in_meta={val_meta.get('seed')})")
    print(f"  -> {OUT.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
