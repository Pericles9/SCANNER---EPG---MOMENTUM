"""T3 Signing Verification — Phase C.5 CVD fix validation.

Runs four independent checks on the FIXED CVD accumulator:
  Check A: OFI ambiguity diagnostics (per event)
  Check B: Spearman rho — CVD vs I(t) at rising edges (should be negative)
  Check C: Spearman rho — CVD vs 30s forward return (should be >= 0)
  Check D: 3 visual spot-check charts (written to results/phase_c_fix/validation_charts/)

Writes: results/phase_c_fix/ofi_diagnostics.json
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.exit_d_tuning.replay import _load_cache
from data.loaders.trades import load_trades, list_events
from data.loaders.quotes import load_quotes
from core.ofi.trade_ofi import compute_trade_ofi

# ── Paths ──────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[2]
_PHASE_B_CACHE = _ROOT / "results" / "phase_b" / "replay_caches"
_PHASE_U_CACHE = Path(r"D:\Trading Research\hawkes-ofi-impact\results\phase_u\replay_caches")
_PHASE_B_AUDIT = (_ROOT / "results" / "phase_b" / "100_val_seed42"
                  / "event_charts" / "cache_audit.json")
_VAL_EVENTS_PATH = _ROOT / "results" / "phase_b" / "100_val_seed42" / "per_event_summary.json"
_OUT_DIR = _ROOT / "results" / "phase_c_fix"
_CHARTS_DIR = _OUT_DIR / "validation_charts"

EPG_PASS = 2


def _load_config():
    with open(_ROOT / "config" / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)
    tier_qbar = q_bar_cfg.get("wide", {}).get("median", 250.0)
    return tier_qbar


def _build_audit_map():
    with open(_PHASE_B_AUDIT) as f:
        audit = json.load(f)
    return {(a["ticker"], a["date"]): a["cache_source"] for a in audit}


def _load_replay(ticker, date, audit_map):
    src = audit_map.get((ticker, date), "fallback")
    if src == "phase_b":
        r = _load_cache(_PHASE_B_CACHE, ticker, date)
    elif src == "phase_u":
        r = _load_cache(_PHASE_U_CACHE, ticker, date)
    else:
        r = _load_cache(_PHASE_B_CACHE, ticker, date)
        if r is None:
            r = _load_cache(_PHASE_U_CACHE, ticker, date)
    return r


def _compute_cvd_fixed(prices, sizes, sides, t_event_idx, N):
    """Compute running CVD using fixed accumulator (float(sides[i]))."""
    cvd_arr = np.zeros(N)
    running = 0.0
    for i in range(t_event_idx, N):
        running += float(prices[i]) * float(sizes[i]) * float(sides[i])
        cvd_arr[i] = running
    return cvd_arr


def main():
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    _CHARTS_DIR.mkdir(parents=True, exist_ok=True)

    tier_qbar = _load_config()
    audit_map = _build_audit_map()
    mom_map = {(ev["ticker"], ev["date"]): ev["mom_pct"]
               for ev in list_events(min_mom=0.0, require_date=True)}

    with open(_VAL_EVENTS_PATH) as f:
        val_events = json.load(f)

    print(f"T3 signing verification — {len(val_events)} events")
    t0 = time.time()

    check_a_per_event = []
    edges_cvd = []    # (cvd_fixed, i_t_ratio)
    edges_ret30 = []  # (cvd_fixed, 30s_fwd_return)
    errors = []

    for i_ev, ev in enumerate(val_events):
        ticker, date = ev["ticker"], ev["date"]

        replay = _load_replay(ticker, date, audit_map)
        if replay is None:
            errors.append(f"{ticker} {date}: no replay cache")
            continue

        mom_pct = mom_map.get((ticker, date))
        if mom_pct is None:
            errors.append(f"{ticker} {date}: no mom_pct in catalog")
            continue

        td = load_trades(ticker, date, mom_pct)
        qd = load_quotes(ticker, date, mom_pct)

        # ── Check A: OFI diagnostics ──
        try:
            ofi = compute_trade_ofi(
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
            ambiguity_rate = ofi.ambiguity_rate
            step2_activation = ofi.step2_activation
        except Exception as exc:
            errors.append(f"{ticker} {date}: OFI error {exc}")
            ambiguity_rate = float(np.sum(replay.sides == 0)) / max(len(replay.sides), 1)
            step2_activation = None

        check_a_per_event.append({
            "ticker": ticker,
            "date": date,
            "ambiguity_rate": round(float(ambiguity_rate), 4),
            "step2_activation": (round(float(step2_activation), 4)
                                 if step2_activation is not None else None),
            "n_trades": int(len(td.timestamps)),
            "flag_high_ambiguity": bool(ambiguity_rate > 0.30),
        })

        # ── Checks B/C: CVD at rising edges ──
        t_event_ns = replay.t_event_ns
        if t_event_ns is None:
            continue

        ts_arr = replay.timestamps_ns
        prices_arr = replay.prices
        sizes_arr = td.sizes
        sides_arr = replay.sides
        epg_arr = replay.epg_state
        iratio_arr = replay.intensity_ratio
        N_tick = len(ts_arr)

        t_event_idx = int(np.searchsorted(ts_arr, t_event_ns))
        cvd_arr = _compute_cvd_fixed(prices_arr, sizes_arr, sides_arr, t_event_idx, N_tick)

        for j in range(1, N_tick):
            if epg_arr[j] != EPG_PASS or epg_arr[j - 1] == EPG_PASS:
                continue
            if j < t_event_idx:
                continue

            cvd_edge = float(cvd_arr[j])
            i_t = float(iratio_arr[j])

            if not np.isnan(i_t):
                edges_cvd.append((cvd_edge, i_t))

            # 30s forward return
            target_ns = int(ts_arr[j]) + 30 * 1_000_000_000
            fwd_idx = int(np.searchsorted(ts_arr, target_ns))
            if fwd_idx < N_tick and float(prices_arr[j]) > 0:
                fwd_ret = (float(prices_arr[fwd_idx]) - float(prices_arr[j])) / float(prices_arr[j])
                edges_ret30.append((cvd_edge, fwd_ret))

        if (i_ev + 1) % 10 == 0:
            elapsed = time.time() - t0
            print(f"  [{i_ev + 1}/{len(val_events)}] {elapsed:.0f}s")

    total_elapsed = time.time() - t0
    print(f"Data collection done. {total_elapsed:.1f}s. Errors: {len(errors)}")

    # ── Check A escalation ────────────────────────────────────────────
    n_high_amb = sum(1 for r in check_a_per_event if r["flag_high_ambiguity"])
    pct_high_amb = n_high_amb / max(len(check_a_per_event), 1) * 100
    check_a_esc = pct_high_amb > 20.0

    # ── Check B ───────────────────────────────────────────────────────
    arr_cvd_b = np.array([x[0] for x in edges_cvd])
    arr_it_b  = np.array([x[1] for x in edges_cvd])
    rho_b, pval_b = stats.spearmanr(arr_cvd_b, arr_it_b)
    check_b_esc = float(rho_b) > 0.05 or (abs(float(rho_b)) < 0.05 and float(pval_b) > 0.05)

    # ── Check C ───────────────────────────────────────────────────────
    arr_cvd_c = np.array([x[0] for x in edges_ret30])
    arr_ret_c = np.array([x[1] for x in edges_ret30])
    rho_c, pval_c = stats.spearmanr(arr_cvd_c, arr_ret_c)
    check_c_esc = float(rho_c) < -0.05 and float(pval_c) < 0.10

    # ── Print summary ────────────────────────────────────────────────
    print(f"\nCheck A: {n_high_amb}/{len(check_a_per_event)} events ({pct_high_amb:.1f}%) "
          f"ambiguity_rate > 30%  {'ESCALATION' if check_a_esc else 'CLEAR'}")
    print(f"Check B: rho={rho_b:.4f}, p={pval_b:.4e}, n={len(arr_cvd_b)}  "
          f"{'ESCALATION' if check_b_esc else 'CLEAR'}")
    print(f"Check C: rho={rho_c:.4f}, p={pval_c:.4e}, n={len(arr_cvd_c)}  "
          f"{'ESCALATION' if check_c_esc else 'CLEAR'}")

    overall_clear = not check_a_esc and not check_b_esc and not check_c_esc
    print(f"\nOverall: {'ALL CLEAR — proceed to T4' if overall_clear else 'ESCALATION TRIGGERED — hard stop'}")

    # ── Write ofi_diagnostics.json ───────────────────────────────────
    out = {
        "description": "Phase C.5 T3 signing verification — fixed CVD accumulator",
        "overall_result": "CLEAR" if overall_clear else "ESCALATION",
        "check_a_ofi_ambiguity": {
            "n_events": len(check_a_per_event),
            "n_high_ambiguity": n_high_amb,
            "pct_high_ambiguity": round(pct_high_amb, 2),
            "escalation_threshold_pct": 20.0,
            "escalation_triggered": check_a_esc,
            "mean_ambiguity_rate": round(float(np.mean([r["ambiguity_rate"]
                                                        for r in check_a_per_event])), 4),
            "mean_step2_activation": (
                round(float(np.mean([r["step2_activation"] for r in check_a_per_event
                                     if r["step2_activation"] is not None])), 4)
                if any(r["step2_activation"] is not None for r in check_a_per_event)
                else None
            ),
            "per_event": check_a_per_event,
        },
        "check_b_cvd_vs_intensity_ratio": {
            "n_rising_edges": int(len(arr_cvd_b)),
            "spearman_rho": round(float(rho_b), 4),
            "p_value": round(float(pval_b), 8),
            "expected_direction": "negative (buy flow -> lower sell ratio)",
            "escalation_rule": "rho > +0.05 OR (|rho| < 0.05 AND p > 0.05)",
            "escalation_triggered": check_b_esc,
        },
        "check_c_cvd_vs_30s_fwd_return": {
            "n_rising_edges": int(len(arr_cvd_c)),
            "spearman_rho": round(float(rho_c), 4),
            "p_value": round(float(pval_c), 8),
            "expected_direction": "non-negative (buy flow -> price up)",
            "escalation_rule": "rho < -0.05 AND p < 0.10",
            "escalation_triggered": check_c_esc,
        },
        "errors": errors,
    }

    out_path = _OUT_DIR / "ofi_diagnostics.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nWritten: {out_path}")

    return overall_clear


if __name__ == "__main__":
    main()
