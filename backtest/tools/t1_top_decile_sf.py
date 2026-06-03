"""
T1 — Build combined cross-stage OPT2 ranking and identify top decile for SF phase.

Merges all non-DQ configs from:
  results/phase_epg_opt2/sweep/stage1_ranked.json   (84 configs)
  results/phase_epg_opt2/sweep/stage2_ranked.json   (189 configs)
  results/phase_epg_opt2/sweep/stage3_fss_ranked.json (144 configs)
  results/phase_epg_opt2/sweep/stage3_fsl_ranked.json (108 configs)

Re-ranks all non-DQ configs jointly by global Borda (CF rank + CR rank).
Top 10% (≈ 52 configs) form the top decile.

Writes:
  results/phase_epg_opt2_sf/combined_ranking.json
  results/phase_epg_opt2_sf/top_decile_configs.json
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

OPT2_SWEEP = REPO_ROOT / "results" / "phase_epg_opt2" / "sweep"
OUT_DIR = REPO_ROOT / "results" / "phase_epg_opt2_sf"

RANKED_FILES = {
    "stage1": OPT2_SWEEP / "stage1_ranked.json",
    "stage2": OPT2_SWEEP / "stage2_ranked.json",
    "stage3_fss": OPT2_SWEEP / "stage3_fss_ranked.json",
    "stage3_fsl": OPT2_SWEEP / "stage3_fsl_ranked.json",
}


def load_non_dq(path: Path, stage: str) -> list[dict]:
    data = json.load(open(path))
    rows = []
    for r in data:
        if r.get("disqualified", False):
            continue
        rows.append({
            "config_id": r["config_id"],
            "stage": stage,
            "variant": r.get("variant", "a"),
            "tau": r.get("tau"),
            "p_open": r.get("p_open"),
            "p_close": r.get("p_close"),
            "m_cool_sec": r.get("m_cool_sec"),
            "tau_cool_sec": r.get("tau_cool_sec"),
            "L_sec": r.get("L_sec"),
            "k_open": r.get("k_open"),
            "k_close": r.get("k_close"),
            "mode": r.get("mode") or ("ss" if r.get("variant") == "f_ss"
                                      else "sl" if r.get("variant") == "f_sl" else None),
            "profit_factor": r["profit_factor"],
            "capture_fraction": r["capture_fraction"],
            "capture_rate": r["capture_rate"],
            "n_trades": r["n_trades"],
            "pass_fraction": r.get("pass_fraction", 0.0),
            "mean_first_entry_delay_sec": r.get("mean_first_entry_delay_sec", 0.0),
        })
    return rows


def borda_rank(items: list[dict], key: str, higher_is_better: bool) -> dict[str, int]:
    sorted_items = sorted(items, key=lambda x: x[key], reverse=higher_is_better)
    return {item["config_id"]: rank + 1 for rank, item in enumerate(sorted_items)}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    all_rows = []
    stage_counts = {}
    for stage, path in RANKED_FILES.items():
        if not path.exists():
            log.warning("Missing: %s", path)
            continue
        rows = load_non_dq(path, stage)
        all_rows.extend(rows)
        stage_counts[stage] = len(rows)
        log.info("  %s: %d non-DQ configs", stage, len(rows))

    total = len(all_rows)
    log.info("Total non-DQ configs: %d", total)

    # Global Borda re-ranking: CF (higher = better) + CR (higher = better)
    rank_cf = borda_rank(all_rows, "capture_fraction", higher_is_better=True)
    rank_cr = borda_rank(all_rows, "capture_rate", higher_is_better=True)

    for row in all_rows:
        cid = row["config_id"]
        row["global_rank_cf"] = rank_cf[cid]
        row["global_rank_cr"] = rank_cr[cid]
        row["global_borda_score"] = rank_cf[cid] + rank_cr[cid]

    # Sort by global Borda ascending (lower = better)
    all_rows.sort(key=lambda x: x["global_borda_score"])
    for rank, row in enumerate(all_rows, 1):
        row["global_borda_rank"] = rank

    # Write combined ranking
    combined_path = OUT_DIR / "combined_ranking.json"
    with open(combined_path, "w") as f:
        json.dump(all_rows, f, indent=2)
    log.info("Written: %s", combined_path)

    # Top decile: top 10%
    n_decile = max(1, int(round(total * 0.10)))
    # Include all tied configs at the boundary
    boundary_score = all_rows[n_decile - 1]["global_borda_score"]
    top_decile = [r for r in all_rows if r["global_borda_score"] <= boundary_score]
    log.info("Top decile: %d configs (boundary borda=%d)", len(top_decile), boundary_score)

    # Stage distribution of top decile
    decile_by_stage: dict[str, int] = {}
    for r in top_decile:
        decile_by_stage[r["stage"]] = decile_by_stage.get(r["stage"], 0) + 1
    log.info("Stage distribution in top decile: %s", decile_by_stage)

    top_decile_path = OUT_DIR / "top_decile_configs.json"
    with open(top_decile_path, "w") as f:
        json.dump(top_decile, f, indent=2)
    log.info("Written: %s", top_decile_path)

    log.info("\nTop 15 by global Borda:")
    for r in top_decile[:15]:
        log.info("  #%d %s [%s] borda=%d CF=%.4f CR=%.6f PF=%.4f n=%d",
                 r["global_borda_rank"], r["config_id"], r["stage"],
                 r["global_borda_score"], r["capture_fraction"],
                 r["capture_rate"], r["profit_factor"], r["n_trades"])


if __name__ == "__main__":
    main()
