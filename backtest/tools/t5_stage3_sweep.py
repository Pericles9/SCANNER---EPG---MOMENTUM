"""
T5 — Stage 3 sweep: 252 SlopeGate configs (144 F_ss + 108 F_sl).

Reads:
  results/phase_epg_grt/train_sample.json  — 300-event training sample (seed=42)

Escalation:
  All F_ss configs DQ'd → hard stop
  All F_sl configs DQ'd → hard stop

Writes:
  results/phase_epg_opt2/sweep/stage3_fss_raw.json
  results/phase_epg_opt2/sweep/stage3_fsl_raw.json
  results/phase_epg_opt2/sweep/stage3_fss_ranked.json
  results/phase_epg_opt2/sweep/stage3_fsl_ranked.json
  results/phase_epg_opt2/sweep/stage3_all_ranked.json
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
from core.epg.gate_variants import SlopeGate
from core.ofi.trade_ofi import compute_trade_ofi
from tools.t3_sweep_runner import (
    _hawkes_replay_with_refit,
    compute_global_fallback_ref,
    EPG_K,
    EPG_WARMUP,
)
from tools.sweep_runner_opt2 import (
    aggregate_config_metrics_opt2,
    dq_and_rank,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

TRAIN_SAMPLE_PATH = REPO_ROOT / "results" / "phase_epg_grt" / "train_sample.json"
OUT_DIR = REPO_ROOT / "results" / "phase_epg_opt2" / "sweep"
N_WORKERS = 8


# ══════════════════════════════════════════════════════════════════════
#  Config grid
# ══════════════════════════════════════════════════════════════════════

def _kc_tag(k_close: float) -> str:
    """Format k_close for config ID (negative uses 'n' prefix)."""
    v = int(round(abs(k_close) * 10))
    return f"kcn{v}" if k_close < 0 else f"kc{v}"


def build_stage3_configs() -> list[dict]:
    """252 Stage 3 configs: 144 F_ss + 108 F_sl."""
    taus = [120, 180, 300]
    L_secs = [30, 60, 90, 120]
    k_opens = [0.5, 1.0, 2.0]
    k_closes = [-1.5, -1.0, -0.5, 0.0]
    p_closes = [0.20, 0.35, 0.50]

    configs = []

    # F_ss — slope open / slope close (144 configs)
    for tau in taus:
        for L in L_secs:
            for ko in k_opens:
                for kc in k_closes:
                    if kc >= ko:
                        continue  # dead band must be positive
                    ko_tag = int(round(ko * 10))
                    config_id = f"s3_fss_t{tau}_l{L}_ko{ko_tag}_{_kc_tag(kc)}"
                    configs.append({
                        "config_id": config_id,
                        "variant": "f_ss",
                        "tau": float(tau),
                        "L_sec": float(L),
                        "k_open": ko,
                        "k_close": kc,
                        "mode": "ss",
                    })

    # F_sl — slope open / level close (108 configs)
    for tau in taus:
        for L in L_secs:
            for ko in k_opens:
                for pc in p_closes:
                    ko_tag = int(round(ko * 10))
                    pc_tag = int(round(pc * 100))
                    config_id = f"s3_fsl_t{tau}_l{L}_ko{ko_tag}_pc{pc_tag}"
                    configs.append({
                        "config_id": config_id,
                        "variant": "f_sl",
                        "tau": float(tau),
                        "L_sec": float(L),
                        "k_open": ko,
                        "p_close": pc,
                        "mode": "sl",
                    })

    return configs


# ══════════════════════════════════════════════════════════════════════
#  Gate replay for SlopeGate
# ══════════════════════════════════════════════════════════════════════

def _run_slope_gate(
    cfg: dict,
    td,
    t_event: float,
    lambda_v_ref: float,
) -> dict:
    """Replay one SlopeGate config on one event. Tracks max_price_during_hold."""
    mode = cfg["mode"]
    gate = SlopeGate(
        tau_sec=cfg["tau"],
        L_sec=cfg["L_sec"],
        k_open=cfg["k_open"],
        mode=mode,
        k_close=cfg.get("k_close", -1.0),
        p_close=cfg.get("p_close", 0.35),
        lambda_v_ref=max(lambda_v_ref, 1e-9),
        warmup_seconds=EPG_WARMUP,
    )
    gate.activate(t_event)

    N = td.n_trades
    prev_state = GateState.INACTIVE
    in_position = False
    entry_t_sec: Optional[float] = None
    entry_price: Optional[float] = None
    position_max_price: Optional[float] = None

    pnl_list: list[float] = []
    hold_list: list[float] = []
    max_price_list: list[float] = []
    entry_price_list: list[float] = []
    pass_windows: list[float] = []
    window_start: Optional[float] = None
    first_entry_delay: Optional[float] = None
    n_pass_ticks = 0
    n_postwarm_ticks = 0

    for i in range(N):
        dv = float(td.prices[i]) * float(td.sizes[i])
        t = td.t_sec[i]

        state = gate.update(dv, t)

        if t >= t_event + EPG_WARMUP and state in (GateState.PASS, GateState.FAIL):
            n_postwarm_ticks += 1
            if state == GateState.PASS:
                n_pass_ticks += 1

        if state == GateState.PASS and prev_state != GateState.PASS:
            window_start = t
        elif state != GateState.PASS and prev_state == GateState.PASS:
            if window_start is not None:
                pass_windows.append(t - window_start)
            window_start = None

        if in_position:
            cur_p = float(td.prices[i])
            if position_max_price is None or cur_p > position_max_price:
                position_max_price = cur_p

        if not in_position:
            rising_edge = (
                state == GateState.PASS
                and prev_state in (GateState.INACTIVE, GateState.WARMUP, GateState.FAIL)
            )
            if rising_edge:
                entry_t_sec = t
                entry_price = float(td.prices[min(i + 1, N - 1)])
                position_max_price = entry_price
                in_position = True
                if first_entry_delay is None:
                    first_entry_delay = t - t_event
        else:
            if prev_state == GateState.PASS and state != GateState.PASS:
                exit_price = float(td.prices[min(i + 1, N - 1)])
                pnl = (exit_price - entry_price) / entry_price * 100.0
                hold = t - entry_t_sec
                pnl_list.append(pnl)
                hold_list.append(hold)
                max_price_list.append(position_max_price if position_max_price is not None else entry_price)
                entry_price_list.append(entry_price)
                in_position = False
                entry_t_sec = None
                entry_price = None
                position_max_price = None

        prev_state = state

    if in_position:
        exit_price = float(td.prices[N - 1])
        pnl = (exit_price - entry_price) / entry_price * 100.0
        hold = td.t_sec[N - 1] - entry_t_sec
        pnl_list.append(pnl)
        hold_list.append(hold)
        max_price_list.append(position_max_price if position_max_price is not None else exit_price)
        entry_price_list.append(entry_price)

    if window_start is not None:
        pass_windows.append(td.t_sec[N - 1] - window_start)

    return {
        "n_trades": len(pnl_list),
        "pnl_list": pnl_list,
        "hold_list": hold_list,
        "max_price_list": max_price_list,
        "entry_price_list": entry_price_list,
        "pass_fraction": n_pass_ticks / n_postwarm_ticks if n_postwarm_ticks > 0 else 0.0,
        "pass_windows": pass_windows,
        "first_entry_delay": first_entry_delay,
    }


def _sweep_worker_stage3(args: dict) -> dict:
    """Multiprocessing worker for Stage 3 SlopeGate sweep."""
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
            return {**base, "status": "skipped", "reason": "insufficient_trades"}

        qd = load_quotes(ticker, date, mom_pct)
        if qd is None or qd.n_quotes < 10:
            return {**base, "status": "skipped", "reason": "insufficient_quotes"}

        N = td.n_trades
        tier_qbar = q_bar_cfg.get("wide", {}).get("median", 250.0)
        ofi_result = compute_trade_ofi(
            trade_timestamps=td.timestamps,
            trade_prices=td.prices,
            trade_sizes=td.sizes.astype(np.float64),
            quote_timestamps=qd.timestamps,
            quote_bid_prices=qd.bid_prices,
            quote_ask_prices=qd.ask_prices,
            quote_bid_sizes=qd.bid_sizes.astype(np.float64),
            quote_ask_sizes=qd.ask_sizes.astype(np.float64),
            window_sec=10.0,
            q_bar_fallback=tier_qbar,
        )
        sides = ofi_result.sides

        lam_buy_out = np.zeros(N, dtype=np.float64)
        lam_sell_out = np.zeros(N, dtype=np.float64)
        E_out = np.zeros(N, dtype=np.float64)
        Edot_out = np.zeros(N, dtype=np.float64)
        n_base_out = np.zeros(N, dtype=np.float64)

        global_lref = fp["mu_buy"] + fp["mu_sell"]
        per_event_lref = compute_lambda_ref_per_event(ticker, date)
        lambda_ref = (
            per_event_lref
            if not math.isnan(per_event_lref) and per_event_lref > 0
            else global_lref
        )

        cold_start_params = _hawkes_replay_with_refit(
            t_sec=td.t_sec, sides=sides,
            rho=rho, lambda_ref=lambda_ref, init_params=fp, rho_E=rho_E,
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
            return {**base, "status": "skipped", "reason": "no_t_event"}

        # lambda_v_ref for SlopeGate normalization: mu_buy + mu_sell from cold-start
        if cold_start_params is not None:
            lv_ref = cold_start_params.mu_buy + cold_start_params.mu_sell
        else:
            lv_ref = fp["mu_buy"] + fp["mu_sell"]
        lv_ref = max(lv_ref, 1e-9)

        results: dict[str, dict] = {}
        for cfg in configs:
            results[cfg["config_id"]] = _run_slope_gate(cfg, td, t_event, lv_ref)

        return {**base, "status": "ok", "results": results}

    except Exception as e:
        import traceback
        return {
            **base, "status": "error",
            "error": str(e), "traceback": traceback.format_exc(),
        }


# ══════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════

def run_subvariant(
    configs: list[dict],
    events: list[dict],
    hawkes_params: dict,
    q_bar_cfg: dict,
    label: str,
    raw_path: Path,
    ranked_path: Path,
) -> list[dict]:
    """Run sweep for one sub-variant (fss or fsl), write files, return ranked rows."""
    rho = hawkes_params.get("rho", 0.99)
    work_items = [
        {
            "ticker": e["ticker"],
            "date": e["date"],
            "mom_pct": e["mom_pct"],
            "hawkes_params": hawkes_params,
            "rho": rho,
            "rho_E": rho,
            "q_bar_cfg": q_bar_cfg,
            "configs": configs,
        }
        for e in events
    ]

    log.info("Starting %s sweep: %d events × %d configs | workers=%d",
             label, len(events), len(configs), N_WORKERS)

    per_config: dict[str, list[dict]] = {c["config_id"]: [] for c in configs}
    n_ok = n_skip = n_err = 0
    t0 = time.time()

    with ProcessPoolExecutor(max_workers=N_WORKERS) as executor:
        futures = {executor.submit(_sweep_worker_stage3, item): item for item in work_items}
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
                log.warning("Error %s %s: %s", r["ticker"], r["date"], r.get("error", "")[:200])

            done = n_ok + n_skip + n_err
            if done % 50 == 0 or done == len(events):
                log.info("  %d/%d (ok=%d skip=%d err=%d) %.1fs",
                         done, len(events), n_ok, n_skip, n_err, time.time() - t0)

    log.info("%s sweep done: ok=%d skip=%d err=%d in %.1fs",
             label, n_ok, n_skip, n_err, time.time() - t0)

    rows = []
    for cfg in configs:
        cid = cfg["config_id"]
        metrics = aggregate_config_metrics_opt2(per_config[cid])
        row = {"config_id": cid, "variant": cfg["variant"], **cfg, **metrics}
        rows.append(row)

    with open(raw_path, "w") as f:
        json.dump({"meta": {"n_configs": len(rows)}, "configs": rows}, f, indent=2)
    log.info("Written: %s", raw_path)

    ranked = dq_and_rank(rows)
    with open(ranked_path, "w") as f:
        json.dump(ranked, f, indent=2)
    log.info("Written: %s", ranked_path)

    return ranked


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with open(TRAIN_SAMPLE_PATH) as f:
        train_data = json.load(f)
    events = train_data["events"]
    log.info("Training events: %d", len(events))

    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_params = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    all_configs = build_stage3_configs()
    fss_configs = [c for c in all_configs if c["variant"] == "f_ss"]
    fsl_configs = [c for c in all_configs if c["variant"] == "f_sl"]
    log.info("F_ss configs: %d, F_sl configs: %d", len(fss_configs), len(fsl_configs))

    fss_ranked_path = OUT_DIR / "stage3_fss_ranked.json"
    if fss_ranked_path.exists():
        log.info("F_ss results already exist — loading from disk")
        fss_ranked = json.load(open(fss_ranked_path))
    else:
        fss_ranked = run_subvariant(
            fss_configs, events, hawkes_params, q_bar_cfg,
            label="F_ss",
            raw_path=OUT_DIR / "stage3_fss_raw.json",
            ranked_path=fss_ranked_path,
        )

    fsl_ranked_path = OUT_DIR / "stage3_fsl_ranked.json"
    if fsl_ranked_path.exists():
        log.info("F_sl results already exist — loading from disk")
        fsl_ranked = json.load(open(fsl_ranked_path))
    else:
        fsl_ranked = run_subvariant(
            fsl_configs, events, hawkes_params, q_bar_cfg,
            label="F_sl",
            raw_path=OUT_DIR / "stage3_fsl_raw.json",
            ranked_path=fsl_ranked_path,
        )

    # Escalation checks
    if all(r["disqualified"] for r in fss_ranked):
        log.error("T5 ESCALATION: all F_ss configs DQ'd.")
        sys.exit(2)
    if all(r["disqualified"] for r in fsl_ranked):
        log.error("T5 ESCALATION: all F_sl configs DQ'd.")
        sys.exit(2)

    # Combined ranked (for chart selection in T7 — top 20 across both)
    all_ranked = sorted(
        [r for r in fss_ranked + fsl_ranked if not r["disqualified"]],
        key=lambda x: x.get("borda_score") or 9999,
    )
    with open(OUT_DIR / "stage3_all_ranked.json", "w") as f:
        json.dump(all_ranked, f, indent=2)
    log.info("Written: %s", OUT_DIR / "stage3_all_ranked.json")

    log.info("\nStage 3 F_ss top 5:")
    for i, r in enumerate([r for r in fss_ranked if not r["disqualified"]][:5], 1):
        log.info("  %2d. %s  borda=%d  CF=%.4f  PF=%.4f", i, r["config_id"],
                 r["borda_score"], r["capture_fraction"], r["profit_factor"])

    log.info("\nStage 3 F_sl top 5:")
    for i, r in enumerate([r for r in fsl_ranked if not r["disqualified"]][:5], 1):
        log.info("  %2d. %s  borda=%d  CF=%.4f  PF=%.4f", i, r["config_id"],
                 r["borda_score"], r["capture_fraction"], r["profit_factor"])


if __name__ == "__main__":
    main()
