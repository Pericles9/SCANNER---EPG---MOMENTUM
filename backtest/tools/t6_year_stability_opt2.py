"""
T6 — Year stability check for Phase EPG-OPT2.

Configs:
  - Top 10 by Borda across Stage 1+2 combined
  - s1_t120_po65_pc65 best cooling combo (if not in top 10)
  - Top 1 F_ss and top 1 F_sl from Stage 3

Years: 2020, 2021, 2022, 2023 (training split only).
Flag regime-sensitive: PF range > 0.60 across years.

Writes:
  results/phase_epg_opt2/year_stability.json
"""
from __future__ import annotations

import json
import logging
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from data.loaders.trades import load_trades, compute_lambda_ref_per_event
from data.loaders.quotes import load_quotes
from data.schemas.mom_db import CONFIG_DIR
from core.epg.anchor import EventAnchor
from core.epg.gate import GateState
from core.ofi.trade_ofi import compute_trade_ofi
from tools.t3_sweep_runner import _hawkes_replay_with_refit, EPG_K
from tools.sweep_runner_opt2 import _run_gate_opt2, aggregate_config_metrics_opt2
from tools.t5_stage3_sweep import _run_slope_gate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

TRAIN_SAMPLE_PATH = REPO_ROOT / "results" / "phase_epg_grt" / "train_sample.json"
STAGE1_RANKED = REPO_ROOT / "results" / "phase_epg_opt2" / "sweep" / "stage1_ranked.json"
STAGE2_RANKED = REPO_ROOT / "results" / "phase_epg_opt2" / "sweep" / "stage2_ranked.json"
STAGE3_FSS_RANKED = REPO_ROOT / "results" / "phase_epg_opt2" / "sweep" / "stage3_fss_ranked.json"
STAGE3_FSL_RANKED = REPO_ROOT / "results" / "phase_epg_opt2" / "sweep" / "stage3_fsl_ranked.json"
OUT_PATH = REPO_ROOT / "results" / "phase_epg_opt2" / "year_stability.json"

N_WORKERS = 8
T120_BASE_ID = "s1_t120_po65_pc65"
REGIME_SENSITIVITY_THRESHOLD = 0.60


def load_configs_for_stability() -> list[dict]:
    """Load and deduplicate stability configs from staged results."""
    configs_by_id: dict[str, dict] = {}

    # Stage 1+2 combined top 10
    s1 = json.load(open(STAGE1_RANKED)) if STAGE1_RANKED.exists() else []
    s2 = json.load(open(STAGE2_RANKED)) if STAGE2_RANKED.exists() else []
    combined = [r for r in s1 + s2 if not r.get("disqualified", False)]
    combined.sort(key=lambda x: x.get("borda_score") or 9999)

    for r in combined[:10]:
        configs_by_id[r["config_id"]] = r

    # t120 best cooling
    t120_cooling = [r for r in s2 if not r.get("disqualified", False)
                    and r.get("base_config_id") == T120_BASE_ID]
    if t120_cooling:
        t120_best = min(t120_cooling, key=lambda x: x.get("borda_score") or 9999)
        configs_by_id.setdefault(t120_best["config_id"], t120_best)

    # Top Stage 3 (1 F_ss + 1 F_sl)
    for path in [STAGE3_FSS_RANKED, STAGE3_FSL_RANKED]:
        if path.exists():
            ranked = json.load(open(path))
            non_dq = [r for r in ranked if not r.get("disqualified", False)]
            if non_dq:
                configs_by_id.setdefault(non_dq[0]["config_id"], non_dq[0])

    return list(configs_by_id.values())


def _worker(args: dict) -> dict:
    """Year-stability worker: same structure as _sweep_worker_opt2."""
    ticker = args["ticker"]
    date = args["date"]
    mom_pct = args["mom_pct"]
    fp = args["hawkes_params"]
    rho = args["rho"]
    rho_E = args["rho_E"]
    q_bar_cfg = args["q_bar_cfg"]
    configs = args["configs"]

    base = {"ticker": ticker, "date": date}
    try:
        td = load_trades(ticker, date, mom_pct)
        if td.n_trades < 30:
            return {**base, "status": "skipped"}
        qd = load_quotes(ticker, date, mom_pct)
        if qd is None or qd.n_quotes < 10:
            return {**base, "status": "skipped"}

        N = td.n_trades
        tier_qbar = q_bar_cfg.get("wide", {}).get("median", 250.0)
        ofi_result = compute_trade_ofi(
            trade_timestamps=td.timestamps, trade_prices=td.prices,
            trade_sizes=td.sizes.astype(np.float64),
            quote_timestamps=qd.timestamps,
            quote_bid_prices=qd.bid_prices, quote_ask_prices=qd.ask_prices,
            quote_bid_sizes=qd.bid_sizes.astype(np.float64),
            quote_ask_sizes=qd.ask_sizes.astype(np.float64),
            window_sec=10.0, q_bar_fallback=tier_qbar,
        )
        sides = ofi_result.sides

        lam_buy_out = np.zeros(N, dtype=np.float64)
        lam_sell_out = np.zeros(N, dtype=np.float64)
        E_out = np.zeros(N, dtype=np.float64)
        Edot_out = np.zeros(N, dtype=np.float64)
        n_base_out = np.zeros(N, dtype=np.float64)

        global_lref = fp["mu_buy"] + fp["mu_sell"]
        per_event_lref = compute_lambda_ref_per_event(ticker, date)
        lambda_ref = (per_event_lref if not math.isnan(per_event_lref) and per_event_lref > 0
                      else global_lref)

        cold_start_params = _hawkes_replay_with_refit(
            t_sec=td.t_sec, sides=sides, rho=rho, lambda_ref=lambda_ref,
            init_params=fp, rho_E=rho_E,
            lam_buy_out=lam_buy_out, lam_sell_out=lam_sell_out,
            E_out=E_out, Edot_out=Edot_out, n_base_out=n_base_out,
        )
        lambda_hat = lam_buy_out + lam_sell_out

        anchor_lref = fp["mu_buy"] + fp["mu_sell"]
        anchor = EventAnchor(lambda_ref=anchor_lref, k_multiplier=EPG_K)
        if cold_start_params is not None:
            lref_epg = cold_start_params.mu_buy + cold_start_params.mu_sell
            if lref_epg > 0:
                anchor.set_lambda_ref(lref_epg)

        t_event = None
        for i in range(N):
            t_ev = anchor.update(lambda_hat[i], td.t_sec[i])
            if t_ev is not None:
                t_event = t_ev
                break
        if t_event is None:
            return {**base, "status": "skipped"}

        lv_ref = (cold_start_params.mu_buy + cold_start_params.mu_sell
                  if cold_start_params is not None else fp["mu_buy"] + fp["mu_sell"])
        lv_ref = max(lv_ref, 1e-9)

        results: dict[str, dict] = {}
        for cfg in configs:
            variant = cfg.get("variant", "a")
            if variant in ("f_ss", "f_sl"):
                results[cfg["config_id"]] = _run_slope_gate(cfg, td, t_event, lv_ref)
            else:
                results[cfg["config_id"]] = _run_gate_opt2(cfg, td, sides, t_event)

        return {**base, "status": "ok", "results": results}
    except Exception as e:
        return {**base, "status": "error", "error": str(e)}


def run_year(year: str, events: list[dict], configs: list[dict],
             hawkes_params: dict, q_bar_cfg: dict) -> dict[str, dict]:
    """Run all configs on year-filtered events. Returns {config_id: metrics}."""
    year_events = [e for e in events if e["date"].startswith(year)]
    if not year_events:
        return {}

    rho = hawkes_params.get("rho", 0.99)
    work_items = [
        {"ticker": e["ticker"], "date": e["date"], "mom_pct": e["mom_pct"],
         "hawkes_params": hawkes_params, "rho": rho, "rho_E": rho,
         "q_bar_cfg": q_bar_cfg, "configs": configs}
        for e in year_events
    ]

    per_config: dict[str, list] = {c["config_id"]: [] for c in configs}
    n_ok = n_skip = n_err = 0

    with ProcessPoolExecutor(max_workers=N_WORKERS) as executor:
        futures = {executor.submit(_worker, item): item for item in work_items}
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

    log.info("  Year %s done: ok=%d skip=%d err=%d", year, n_ok, n_skip, n_err)

    return {
        cid: aggregate_config_metrics_opt2(per_config[cid])
        for cid in per_config
    }


def main() -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    with open(TRAIN_SAMPLE_PATH) as f:
        train_data = json.load(f)
    events = train_data["events"]

    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_params = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    configs = load_configs_for_stability()
    log.info("Year stability configs: %d", len(configs))
    for c in configs:
        log.info("  %s", c["config_id"])

    years = ["2020", "2021", "2022", "2023"]
    year_results: dict[str, dict] = {}

    for year in years:
        log.info("Year %s...", year)
        year_results[year] = run_year(year, events, configs, hawkes_params, q_bar_cfg)

    stability_rows = []
    for cfg in configs:
        cid = cfg["config_id"]
        per_year = {}
        pf_values = []
        for yr in years:
            m = year_results.get(yr, {}).get(cid, {})
            per_year[yr] = {
                "n_trades": m.get("n_trades", 0),
                "profit_factor": m.get("profit_factor", 0.0),
                "win_rate": m.get("win_rate", 0.0),
                "mean_pnl_pct": m.get("mean_pnl_pct", 0.0),
                "capture_fraction": m.get("capture_fraction", 0.0),
                "capture_rate": m.get("capture_rate", 0.0),
            }
            pf = m.get("profit_factor", 0.0)
            if m.get("n_trades", 0) > 0:
                pf_values.append(pf)

        pf_range = max(pf_values) - min(pf_values) if len(pf_values) >= 2 else 0.0
        regime_sensitive = pf_range > REGIME_SENSITIVITY_THRESHOLD

        log.info("%s: %s", cid, " ".join(
            f"{yr}=PF{per_year[yr]['profit_factor']:.2f}" for yr in years
        ))
        if regime_sensitive:
            log.warning("  ^ REGIME SENSITIVE (PF range=%.3f)", pf_range)

        stability_rows.append({
            "config_id": cid,
            "variant": cfg.get("variant", "a"),
            "per_year": per_year,
            "pf_range": round(pf_range, 4),
            "regime_sensitive": regime_sensitive,
        })

    with open(OUT_PATH, "w") as f:
        json.dump(stability_rows, f, indent=2)
    log.info("Written: %s", OUT_PATH)


if __name__ == "__main__":
    main()
