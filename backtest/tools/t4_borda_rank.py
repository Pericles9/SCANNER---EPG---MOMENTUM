"""
T4 — Borda rank aggregation on T3 sweep results.

Reads results/phase_epg_grt/sweep/all_configs.json (129 configs).
Disqualifies: pass_fraction < 7% OR n_trades < 50.
Ranks by: PF (rank_pf), mean_pnl_pct (rank_pnl), capture_rate (rank_cr).
borda_score = rank_pf + rank_pnl + rank_cr  (lower = better, 1-indexed).

Selection:
  - Top 3 non-disqualified configs (any variant) by borda_score
  - Per-variant best (5 rows; one per variant a-e), excluding top-3 already selected
  - Also always includes baseline: var_a_t300_po65_pc65

Outputs:
  results/phase_epg_grt/ranked_all.json    — all 129 rows with ranks/borda
  results/phase_epg_grt/selection.json     — selected configs (top3 + per-variant-best)
  config/phase_epg_grt/{config_id}.json    — full strategy config for each selected config
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

SWEEP_DIR = REPO_ROOT / "results" / "phase_epg_grt" / "sweep"
OUT_DIR = REPO_ROOT / "results" / "phase_epg_grt"
CONFIG_OUT_DIR = REPO_ROOT / "config" / "phase_epg_grt"
BASE_CONFIG_PATH = REPO_ROOT / "config" / "phase_f.json"

BASELINE_ID = "var_a_t300_po65_pc65"

PASS_FRACTION_MIN = 0.07
N_TRADES_MIN = 50


def load_all_configs() -> list[dict]:
    path = SWEEP_DIR / "all_configs.json"
    if not path.exists():
        log.error("all_configs.json not found at %s — run T3 first", path)
        sys.exit(1)
    with open(path) as f:
        data = json.load(f)
    # T3 writes {"meta": {...}, "configs": [...]}
    if isinstance(data, dict) and "configs" in data:
        return data["configs"]
    return data  # fallback: already a list


def borda_rank(rows: list[dict], key: str, higher_is_better: bool) -> dict[str, int]:
    """Return {config_id: rank} for `key` (1 = best)."""
    sorted_rows = sorted(rows, key=lambda r: r[key], reverse=higher_is_better)
    return {r["config_id"]: i + 1 for i, r in enumerate(sorted_rows)}


def main() -> None:
    all_rows = load_all_configs()
    log.info("Loaded %d configs from all_configs.json", len(all_rows))

    # ── Disqualification ──
    for row in all_rows:
        pf = row.get("pass_fraction", 0.0)
        nt = row.get("n_trades", 0)
        row["disqualified"] = (pf < PASS_FRACTION_MIN) or (nt < N_TRADES_MIN)
        row["dq_reason"] = []
        if pf < PASS_FRACTION_MIN:
            row["dq_reason"].append(f"pass_fraction={pf:.3f} < {PASS_FRACTION_MIN}")
        if nt < N_TRADES_MIN:
            row["dq_reason"].append(f"n_trades={nt} < {N_TRADES_MIN}")

    n_dq = sum(1 for r in all_rows if r["disqualified"])
    log.info("Disqualified: %d/%d configs", n_dq, len(all_rows))

    # ── Borda ranking (applied to ALL configs including disqualified) ──
    rank_pf = borda_rank(all_rows, "profit_factor", higher_is_better=True)
    rank_pnl = borda_rank(all_rows, "mean_pnl_pct", higher_is_better=True)
    rank_cr = borda_rank(all_rows, "capture_rate", higher_is_better=True)

    for row in all_rows:
        cid = row["config_id"]
        row["rank_pf"] = rank_pf[cid]
        row["rank_pnl"] = rank_pnl[cid]
        row["rank_cr"] = rank_cr[cid]
        row["borda_score"] = row["rank_pf"] + row["rank_pnl"] + row["rank_cr"]

    # Sort by borda_score ascending (lower = better)
    all_rows.sort(key=lambda r: r["borda_score"])

    # ── Write ranked_all.json ──
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ranked_path = OUT_DIR / "ranked_all.json"
    with open(ranked_path, "w") as f:
        json.dump(all_rows, f, indent=2)
    log.info("Written: %s", ranked_path)

    # ── Selection: top 3 non-disqualified ──
    qualified = [r for r in all_rows if not r["disqualified"]]
    if len(qualified) == 0:
        log.error("T4 ESCALATION: no qualified configs (all DQ'd). Cannot select.")
        sys.exit(2)

    top3 = qualified[:3]
    top3_ids = {r["config_id"] for r in top3}
    log.info("Top 3 by Borda score:")
    for i, r in enumerate(top3, 1):
        log.info(
            "  #%d %s — borda=%d (PF=%.4f, pnl=%.4f%%, cr=%.6f) dq=%s",
            i, r["config_id"], r["borda_score"],
            r.get("profit_factor", 0), r.get("mean_pnl_pct", 0),
            r.get("capture_rate", 0), r["disqualified"],
        )

    # ── Per-variant best (from qualified, excluding top3) ──
    per_variant_best: dict[str, dict] = {}
    for r in qualified:
        v = r["variant"]
        if r["config_id"] in top3_ids:
            continue
        if v not in per_variant_best:
            per_variant_best[v] = r

    log.info("Per-variant best (excl. top 3):")
    for v in sorted(per_variant_best):
        r = per_variant_best[v]
        log.info("  variant_%s: %s — borda=%d", v, r["config_id"], r["borda_score"])

    # ── Ensure baseline is included ──
    baseline_row = next((r for r in all_rows if r["config_id"] == BASELINE_ID), None)
    if baseline_row is None:
        log.warning("Baseline config %s not found in sweep results", BASELINE_ID)

    # ── Build selection list ──
    selected_ids = set()
    selection = []

    for role, row in [("top_1", top3[0]), ("top_2", top3[1]), ("top_3", top3[2])]:
        if row["config_id"] not in selected_ids:
            row["selection_role"] = role
            selection.append(row)
            selected_ids.add(row["config_id"])

    for v in sorted(per_variant_best):
        row = per_variant_best[v]
        if row["config_id"] not in selected_ids:
            row["selection_role"] = f"per_variant_best_{v}"
            selection.append(row)
            selected_ids.add(row["config_id"])

    if baseline_row and baseline_row["config_id"] not in selected_ids:
        baseline_row["selection_role"] = "baseline"
        selection.append(baseline_row)
        selected_ids.add(baseline_row["config_id"])
    elif baseline_row:
        # Already in selection — tag it additionally
        for row in selection:
            if row["config_id"] == BASELINE_ID:
                row["selection_role"] += "+baseline"

    sel_path = OUT_DIR / "selection.json"
    with open(sel_path, "w") as f:
        json.dump(selection, f, indent=2)
    log.info("Written: %s (%d configs)", sel_path, len(selection))

    # ── Write config JSONs for each selected config ──
    with open(BASE_CONFIG_PATH) as f:
        base_cfg = json.load(f)

    CONFIG_OUT_DIR.mkdir(parents=True, exist_ok=True)

    for row in selection:
        cfg = _build_strategy_config(base_cfg, row)
        cfg_path = CONFIG_OUT_DIR / f"{row['config_id']}.json"
        with open(cfg_path, "w") as f:
            json.dump(cfg, f, indent=2)
        log.info("  config written: %s", cfg_path)

    log.info("\nT4 complete:")
    log.info("  ranked_all.json: %d configs", len(all_rows))
    log.info("  selection.json: %d configs", len(selection))
    log.info("  config JSONs: %d files in config/phase_epg_grt/", len(selection))


def _build_strategy_config(base: dict, row: dict) -> dict:
    """Build a full strategy config from the base config + gate sweep row."""
    import copy
    cfg = copy.deepcopy(base)

    # Disable all exits except EPG window close (as in T3 sweep)
    cfg["exit_d"]["enabled"] = False
    cfg["luld"]["enabled"] = False
    cfg.setdefault("reentry", {})["enabled"] = False
    cfg.setdefault("gap_gate", {})["enabled"] = False
    cfg.setdefault("intra_window_watermark", {})["enabled"] = False

    variant = row["variant"]
    cfg["epg_gate_variant"] = variant
    cfg["config_id"] = row["config_id"]

    if variant == "a":
        cfg["epg"] = cfg.get("epg", {})
        cfg["epg"]["tau"] = row["tau"]
        cfg["epg"]["p_open"] = row["p_open"]
        cfg["epg"]["p_close"] = row["p_close"]
    elif variant == "b":
        cfg["epg"] = cfg.get("epg", {})
        cfg["epg"]["k_abs"] = row["k_abs"]
        cfg["epg"]["tau"] = row.get("tau", 300)
    elif variant == "c":
        cfg["epg"] = cfg.get("epg", {})
        cfg["epg"]["beta_slow"] = row["beta_slow"]
        cfg["epg"]["k_slow"] = row["k_slow"]
    elif variant == "d":
        cfg["epg"] = cfg.get("epg", {})
        cfg["epg"]["beta_slow"] = row["beta_slow"]
        cfg["epg"]["k_slow"] = row["k_slow"]
    elif variant == "e":
        cfg["epg"] = cfg.get("epg", {})
        cfg["epg"]["window_n"] = row["window_n"]
        cfg["epg"]["threshold_r"] = row["threshold_r"]

    cfg["_phase"] = "epg_grt"
    cfg["_sweep_metrics"] = {
        k: row.get(k)
        for k in ["profit_factor", "n_trades", "win_rate", "mean_pnl_pct",
                  "mean_hold_sec", "pass_fraction", "mean_window_duration_sec",
                  "mean_first_entry_delay_sec", "capture_rate",
                  "borda_score", "rank_pf", "rank_pnl", "rank_cr"]
    }
    return cfg


if __name__ == "__main__":
    main()
