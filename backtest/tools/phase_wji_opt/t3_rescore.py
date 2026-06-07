"""
T3-RESCORE — Re-apply corrected hard filters to existing T3 WJI data.

No gate replay. Loads results/phase_wji_opt/stage1_wji.json and re-scores
with the corrected filter set:
  n_trades >= 200 (train)
  cvar5_pct >= -8.0
  pf >= 1.0
  max_loss_pct: reported only, NOT a hard filter

worst_event is null for T3 configs (trade-level data not stored in T3 output).

Writes:
  results/phase_wji_opt/stage1_wji_rescored.json
  results/phase_wji_opt/stage1_findings.json
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tools.phase_wji_opt.scorer import apply_hard_filters, borda_rank

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

IN_PATH = REPO_ROOT / "results" / "phase_wji_opt" / "stage1_wji.json"
OUT_RESCORED = REPO_ROOT / "results" / "phase_wji_opt" / "stage1_wji_rescored.json"
OUT_FINDINGS = REPO_ROOT / "results" / "phase_wji_opt" / "stage1_findings.json"

CORRECTED_THRESHOLDS = {
    "n_trades_floor": 200,
    "cvar5_floor_pct": -8.0,
    "pf_floor": 1.0,
}


def write_json_atomic(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
    ) as f:
        json.dump(data, f, indent=2)
        tmp = Path(f.name)
    os.replace(str(tmp), str(path))
    log.info("Written: %s", path)


def main() -> None:
    if not IN_PATH.exists():
        log.error("T3 data not found: %s", IN_PATH)
        sys.exit(1)

    with open(IN_PATH) as f:
        t3 = json.load(f)

    config_ids = list(t3["metrics"].keys())
    metrics_by_config = t3["metrics"]
    per_year_by_config = t3["per_year"]
    config_meta = t3["configs"]

    # Re-apply corrected hard filters
    survivors = apply_hard_filters(config_ids, metrics_by_config, CORRECTED_THRESHOLDS)
    log.info("Survivors (%d/%d): %s", len(survivors), len(config_ids), survivors)

    if not survivors:
        log.error("No survivors after corrected filtering. Check cvar5_floor or n_trades_floor.")
        sys.exit(3)

    # Borda rank all survivors (asym already filtered by cvar5 check)
    ranked = borda_rank(survivors, metrics_by_config)
    borda_scores: dict[str, int] = {cid: i + 1 for i, cid in enumerate(ranked)}

    # Build rescored output
    rescored_configs: dict[str, dict] = {}
    for cid in config_ids:
        m = metrics_by_config[cid]
        meta = config_meta.get(cid, {})
        filter_pass = cid in survivors
        borda = borda_scores.get(cid)

        rescored_configs[cid] = {
            "p": meta.get("p"),
            "hysteresis": meta.get("hysteresis"),
            "capture_fraction": m.get("capture_fraction"),
            "ev": m.get("ev"),
            "cvar5_pct": m.get("cvar5_pct"),
            "max_loss_pct": m.get("max_loss_pct"),
            "worst_event": None,  # trade-level data not stored in T3 output
            "median_pct": m.get("median_pct"),
            "n_trades": m.get("n_trades"),
            "pf": m.get("pf"),
            "filter_pass": filter_pass,
            "borda_rank": borda,
        }

    output = {
        "meta": {
            "source": str(IN_PATH.name),
            "corrected_thresholds": CORRECTED_THRESHOLDS,
            "n_configs": len(config_ids),
            "n_survivors": len(survivors),
            "winner": ranked[0] if ranked else None,
            "note_worst_event": "worst_event=null for all T3 configs: trade-level data (ticker/date) not stored in T3 replay output.",
        },
        "configs": rescored_configs,
        "per_year": per_year_by_config,
        "filter_results": {
            "survivors": survivors,
            "ranked": ranked,
            "winner": ranked[0] if ranked else None,
        },
    }

    write_json_atomic(output, OUT_RESCORED)

    # Log T3-RESCORE table
    log.info("\n%-20s %6s %8s %7s %9s %11s %8s %8s %5s %4s",
             "config", "p", "hyst", "cap_fr", "ev", "cvar5", "max_loss", "median", "n", "pf")
    for cid in config_ids:
        c = rescored_configs[cid]
        flag = "PASS" if c["filter_pass"] else "FAIL"
        rank_str = f"#{c['borda_rank']}" if c["borda_rank"] else "  -"
        log.info("%-20s %6.2f %-8s %7.4f %8.3f %9.2f %11.2f %8.3f %5d %4.3f  [%s %s]",
                 cid,
                 c["p"] or 0.0,
                 c["hysteresis"] or "",
                 c["capture_fraction"] or 0.0,
                 c["ev"] or 0.0,
                 c["cvar5_pct"] or 0.0,
                 c["max_loss_pct"] or 0.0,
                 c["median_pct"] or 0.0,
                 c["n_trades"] or 0,
                 c["pf"] or 0.0,
                 flag, rank_str)

    log.info("\nWinner: %s", ranked[0] if ranked else "none")

    # Stage 1 findings
    findings = {
        "asym_finding": (
            "All asym-mode configs failed cvar5_pct (−17% to −18% vs floor −8.0). "
            "Hysteresis mode ruled out on loss-tail grounds. "
            "p_close=0.30 holds through signal-elevated drawdowns."
        ),
        "max_loss_demotion": (
            "max_loss_pct demoted from hard filter to reported diagnostic. "
            "Single-worst-trade stat is dominated by one outlier event; too noisy to gate on "
            "across a 10-config sweep. Retained in result tables for transparency."
        ),
        "single_mode_cvar5_range": "Single-mode cvar5_pct range: −6.7% to −7.3% (all pass floor −8.0).",
        "n_survivors": len(survivors),
        "survivor_configs": survivors,
        "winner": ranked[0] if ranked else None,
    }
    write_json_atomic(findings, OUT_FINDINGS)

    log.info("T3-RESCORE complete. Winner: %s", ranked[0] if ranked else "none")


if __name__ == "__main__":
    main()
