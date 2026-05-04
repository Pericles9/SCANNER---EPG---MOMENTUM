#!/usr/bin/env python3
"""Phase S — EPG replay helper for chart generation.

Replays the same Hawkes + EPG sequence as `backtest/runner_screening_only.py`
but instruments the inner loop to record `lambda_v`, `running_peak`,
`threshold`, and `state` at every trade tick. This is the data the per-event
charts need to render the lambda_V panel and the EPG-state step trace.

Public API:
    replay_epg_for_event(ticker, date, mom_pct=None) -> dict

Returns:
    {
      "t_event": int | None,        # absolute timestamp (ns UTC) at fire, or None
      "epg_timeline": list[dict],   # one entry per tick
                                     #   {ts, lambda_v, running_peak,
                                     #    threshold, state}
      "pass_windows": list[dict],   # {open_ts, close_ts}, both ns UTC
    }
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.schemas.mom_db import CONFIG_DIR
from data.loaders.trades import (
    load_trades, list_events, compute_lambda_ref_per_event,
)
from data.loaders.quotes import load_quotes
from core.ofi.trade_ofi import compute_trade_ofi
from core.epg.anchor import EventAnchor
from core.epg.gate import ParticipationGate, GateState

# Reuse the production Hawkes refit machinery
from backtest.runner import _hawkes_replay_with_refit

# Match Phase S exactly
EPG_K = 5
EPG_TAU = 300.0
EPG_P = 0.65
EPG_WARMUP = 300.0


def _resolve_mom_pct(ticker: str, date: str) -> float:
    """Look up the mom_pct suffix for an event from the catalog."""
    for ev in list_events(min_mom=0.0, require_date=True):
        if ev["ticker"] == ticker and ev["date"] == date:
            return ev["mom_pct"]
    raise FileNotFoundError(f"No event in catalog for {ticker} {date}")


def replay_epg_for_event(
    ticker: str,
    date: str,
    mom_pct: Optional[float] = None,
) -> dict:
    """Run the Phase S Hawkes + EPG sequence and instrument per-tick state.

    Loads trades + quotes for one event, runs Lee-Ready classification, the
    cold-start + chunked-refit Hawkes replay, and the EventAnchor /
    ParticipationGate loop -- the same wiring as `runner_screening_only.py`.
    Records (lambda_v, running_peak, threshold, state) at every trade tick so
    the chart panels can render exactly what happened during the backtest.
    """
    if mom_pct is None:
        mom_pct = _resolve_mom_pct(ticker, date)

    # ── Load data ──
    td = load_trades(ticker, date, mom_pct)
    qd = load_quotes(ticker, date, mom_pct)
    if td.n_trades < 30 or qd.n_quotes < 10:
        return {"t_event": None, "epg_timeline": [], "pass_windows": []}

    # ── Hawkes config ──
    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_median = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    # Per-event Hawkes params from Phase A (if available)
    phase_a_path = Path(__file__).resolve().parent.parent / "results" / "phase_a" / "production_fit_results.json"
    fp = hawkes_median
    if phase_a_path.exists():
        with open(phase_a_path) as f:
            phase_a_results = json.load(f)
        for r in phase_a_results:
            if (r.get("status") == "success" and r.get("ticker") == ticker
                    and r.get("date") == date and "final_params" in r):
                fp = r["final_params"]
                break

    rho = hawkes_median.get("rho", 0.99)
    rho_E = rho

    # ── Lee-Ready sides ──
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

    # ── Hawkes replay with online refit ──
    N = td.n_trades
    lam_buy_out = np.zeros(N, dtype=np.float64)
    lam_sell_out = np.zeros(N, dtype=np.float64)
    E_out = np.zeros(N, dtype=np.float64)
    Edot_out = np.zeros(N, dtype=np.float64)
    n_base_out = np.zeros(N, dtype=np.float64)

    global_lambda_ref = fp["mu_buy"] + fp["mu_sell"]
    per_event_lref = compute_lambda_ref_per_event(ticker, date)
    if math.isnan(per_event_lref) or per_event_lref <= 0:
        lambda_ref = global_lambda_ref
    else:
        lambda_ref = per_event_lref

    cold_start_params = _hawkes_replay_with_refit(
        t_sec=td.t_sec, sides=sides,
        rho=rho, lambda_ref=lambda_ref,
        init_params=fp, rho_E=rho_E,
        lam_buy_out=lam_buy_out, lam_sell_out=lam_sell_out,
        E_out=E_out, Edot_out=Edot_out, n_base_out=n_base_out,
    )
    lambda_hat = lam_buy_out + lam_sell_out

    # ── EventAnchor + ParticipationGate ──
    global_lref_epg = fp["mu_buy"] + fp["mu_sell"]
    anchor = EventAnchor(lambda_ref=global_lref_epg, k_multiplier=EPG_K)
    if cold_start_params is not None:
        lref_epg = cold_start_params.mu_buy + cold_start_params.mu_sell
        if lref_epg > 0:
            anchor.set_lambda_ref(lref_epg)
    gate = ParticipationGate(
        half_life_seconds=EPG_TAU,
        peak_threshold_p=EPG_P,
        warmup_seconds=EPG_WARMUP,
    )

    epg_timeline: list[dict] = []
    pass_windows: list[dict] = []
    pass_open_ts: Optional[int] = None
    t_event_fired = False
    t_event_ns: Optional[int] = None

    for i in range(N):
        t_ev = anchor.update(lambda_hat[i], td.t_sec[i])
        if t_ev is not None and not t_event_fired:
            gate.activate(t_ev)
            t_event_fired = True
            t_event_ns = int(td.timestamps[i])

        dv = float(td.prices[i]) * float(td.sizes[i])
        state = gate.update(dv, td.t_sec[i])

        ts_ns = int(td.timestamps[i])
        epg_timeline.append({
            "ts": ts_ns,
            "lambda_v": float(gate.lambda_v),
            "running_peak": float(gate.lambda_v_peak),
            "threshold": float(gate.threshold),
            "state": state.value,
        })

        # Track contiguous PASS windows
        if state == GateState.PASS and pass_open_ts is None:
            pass_open_ts = ts_ns
        elif state != GateState.PASS and pass_open_ts is not None:
            pass_windows.append({"open_ts": pass_open_ts, "close_ts": ts_ns})
            pass_open_ts = None

    # If event ends mid-PASS, close the window at the last tick
    if pass_open_ts is not None:
        pass_windows.append({"open_ts": pass_open_ts,
                             "close_ts": int(td.timestamps[N - 1])})

    return {
        "t_event": t_event_ns,
        "epg_timeline": epg_timeline,
        "pass_windows": pass_windows,
    }


# ── T2a smoke test ────────────────────────────────────────────────────

def _smoke_test():
    """T2a: replay on the first 3 events alphabetically from per_event_summary.json."""
    import pandas as pd
    base = Path(__file__).resolve().parent.parent / "results" / "backtest"
    with open(base / "per_event_summary.json") as f:
        events = json.load(f)
    events = sorted(events, key=lambda e: (e["ticker"], e["date"]))[:3]

    for ev in events:
        ticker, date = ev["ticker"], ev["date"]
        print(f"\n=== {ticker} {date} ===")
        result = replay_epg_for_event(ticker, date)
        n_pass = len(result["pass_windows"])
        t_event_ns = result["t_event"]
        if t_event_ns is None:
            print(f"  T_event: None  *** UNEXPECTED ***")
        else:
            t_event_et = pd.Timestamp(t_event_ns, unit="ns",
                                      tz="UTC").tz_convert("America/New_York")
            print(f"  T_event ns: {t_event_ns}  ET: {t_event_et}")
        print(f"  PASS windows: {n_pass}")
        print(f"  epg_timeline length: {len(result['epg_timeline'])}")
        print(f"  first 5 timeline rows:")
        for row in result["epg_timeline"][:5]:
            ts_et = pd.Timestamp(row["ts"], unit="ns",
                                 tz="UTC").tz_convert("America/New_York")
            print(f"    ts={ts_et} lambda_v={row['lambda_v']:.4g} "
                  f"peak={row['running_peak']:.4g} thresh={row['threshold']:.4g} "
                  f"state={row['state']}")


if __name__ == "__main__":
    _smoke_test()
