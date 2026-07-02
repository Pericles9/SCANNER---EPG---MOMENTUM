#!/usr/bin/env python3
"""
Phase Tail-Risk · Structural Mitigation — T1 canonical extraction.

Single source of truth for the whole sub-phase. For every trade in the R1-Final
(p=0.80, NO time gate) baseline, extracts the raw price path from entry through exit,
derives MAE/MFE, entry bid-ask spread, and the 60s staged-entry checkpoint, and stores
everything in raw_trajectories.json. Every later task (T2-T7) reads from that file and
does NOT re-pull tick/quote parquet (redundancy rule).

Execution convention (mirrors runner_rapid.py exactly):
  entry fills at tick entry_idx+1 (runner_rapid.py:657)
  epg_window_close exit fills at exit_idx+1 (:713); session_end at N-1 (:753)
  MAE/MFE = min/max running PnL% over the hold, anchored at entry_price.

Gate note: in the no-gate baseline every exit is epg_window_close or session_end, so the
gate is PASS for the entire hold by construction. Therefore "gate still PASS at 60s" is
exactly equivalent to hold_sec >= 60 (recorded as gate_pass_at_60s).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

BACKTEST = Path(__file__).resolve().parent.parent
if str(BACKTEST) not in sys.path:
    sys.path.insert(0, str(BACKTEST))

from data.loaders.trades import load_trades
from data.loaders.quotes import load_quotes

RESULTS = BACKTEST / "results"
SAMPLE = BACKTEST / "data" / "val_r4_stratified.json"
PER_TRADE = RESULTS / "phase_r1_final" / "sym_p80" / "per_trade.json"
OUT_DIR = RESULTS / "phase_tail_risk" / "structural_mitigation"
NS = 1_000_000_000
STAGE_CHECKPOINT_SEC = 60.0
DOWNSAMPLE_SEC = 30.0


def _entry_spread(qd, entry_ts):
    """Prevailing bid-ask spread at entry (%, of mid). Last quote at/before entry_ts."""
    idx = int(np.searchsorted(qd.timestamps, entry_ts, side="right")) - 1
    if idx < 0:
        idx = 0
    bid, ask = qd.bid_prices[idx], qd.ask_prices[idx]
    mid = (bid + ask) / 2.0
    if mid <= 0 or ask < bid:
        return None, None
    spr_pct = (ask - bid) / mid * 100.0
    return round(float(spr_pct), 4), round(float(mid), 4)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    smp = {(e["ticker"], e["date"]): e for e in json.load(open(SAMPLE))["events"]}
    trades = json.load(open(PER_TRADE))
    print(f"T1: extracting raw trajectories for {len(trades)} baseline trades")

    records = []
    max_recon_err = 0.0
    for t in trades:
        key = (t["ticker"], t["date"])
        ev = smp.get(key, {})
        mom = ev.get("mom_pct")
        td = load_trades(t["ticker"], t["date"], mom)

        entry_idx = int(t["entry_idx"])
        exit_idx = int(t["exit_idx"])
        entry_price = float(t["entry_price"])
        reason = t["exit_reason"]
        N = len(td.prices)

        # exit fill index per reason (matches runner)
        if reason == "epg_window_close":
            exit_fill = min(exit_idx + 1, N - 1)
        else:  # session_end (baseline has no time_gate)
            exit_fill = min(exit_idx, N - 1)

        f0 = min(entry_idx + 1, N - 1)  # entry fill tick
        seg_price = td.prices[f0:exit_fill + 1].astype(np.float64)
        seg_tsec = td.t_sec[f0:exit_fill + 1].astype(np.float64)
        if len(seg_price) == 0:
            print(f"  ! empty segment {key}, skipping")
            continue
        run_pnl = (seg_price - entry_price) / entry_price * 100.0
        entry_tsec = float(td.t_sec[entry_idx])
        rel = seg_tsec - entry_tsec

        mfe = float(np.max(run_pnl))
        mae = float(np.min(run_pnl))
        mfe_time = float(rel[int(np.argmax(run_pnl))])
        mae_time = float(rel[int(np.argmin(run_pnl))])
        final_pnl = float(run_pnl[-1])
        recon_err = abs(final_pnl - float(t["pnl_pct"]))
        max_recon_err = max(max_recon_err, recon_err)

        # MAE envelope: running-min "new low" points — sufficient to find first
        # breach of any stop level L exactly (cummin is monotone).
        envelope = []
        cur_min = np.inf
        for r_t, p, pr in zip(rel, run_pnl, seg_price):
            if p < cur_min - 1e-9:
                cur_min = p
                envelope.append([round(float(r_t), 2), round(float(p), 4), round(float(pr), 5)])

        # 30s downsampled path for charts
        traj = []
        gi = 0
        for g in np.arange(0, rel[-1] + DOWNSAMPLE_SEC, DOWNSAMPLE_SEC):
            j = int(np.searchsorted(rel, g, side="right")) - 1
            if j < 0:
                j = 0
            traj.append([round(float(g), 1), round(float(run_pnl[j]), 4)])

        # 60s staged-entry checkpoint
        hold = float(t["hold_sec"])
        gate_pass_at_60s = hold >= STAGE_CHECKPOINT_SEC
        if gate_pass_at_60s:
            j60 = int(np.searchsorted(rel, STAGE_CHECKPOINT_SEC, side="left"))
            j60 = min(j60, len(seg_price) - 1)
            price_at_60s = float(seg_price[j60])
            pnl_at_60s = float(run_pnl[j60])
        else:
            price_at_60s = None
            pnl_at_60s = None

        # entry spread
        try:
            qd = load_quotes(t["ticker"], t["date"], mom)
            spr_pct, mid = _entry_spread(qd, int(t["entry_ts"]))
        except Exception as e:
            print(f"  ! quotes failed {key}: {e}")
            spr_pct, mid = None, None

        records.append({
            "ticker": t["ticker"], "date": t["date"],
            "stratum": ev.get("stratum", "unknown"),
            "session_bucket": t["session_bucket"],
            "exit_reason": reason,
            "entry_idx": entry_idx, "exit_idx": exit_idx, "exit_fill_idx": exit_fill,
            "entry_ts": int(t["entry_ts"]), "exit_ts": int(t["exit_ts"]),
            "entry_t_sec": entry_tsec, "hold_sec": hold,
            "entry_price": entry_price, "exit_price": float(t["exit_price"]),
            "baseline_pnl_pct": float(t["pnl_pct"]),
            "is_winner": bool(t["pnl_pct"] > 0),
            "mfe_pct": round(mfe, 4), "mae_pct": round(mae, 4),
            "mfe_time_sec": round(mfe_time, 1), "mae_time_sec": round(mae_time, 1),
            "recon_err": round(recon_err, 5),
            "entry_spread_pct": spr_pct, "entry_mid": mid,
            "n_halt_windows": int(t["n_halt_windows"]),
            "halt_overlap": bool(t["n_halt_windows"] > 0),
            "gate_pass_at_60s": gate_pass_at_60s,
            "price_at_60s": price_at_60s, "pnl_at_60s": pnl_at_60s,
            "mae_envelope": envelope,   # [t_rel, pnl, price] new-low points
            "traj_30s": traj,           # [t_rel, pnl] for charts
        })

    out = {
        "meta": {
            "baseline": "R1-Final sym_p80 (no time gate)",
            "n_trades": len(records),
            "execution_convention": "entry fill=entry_idx+1; epg_window_close exit=exit_idx+1; session_end=N-1",
            "gate_note": "no-gate baseline: gate PASS entire hold => gate_pass_at_60s == (hold_sec>=60)",
            "stage_checkpoint_sec": STAGE_CHECKPOINT_SEC,
            "max_recon_err_pct": round(max_recon_err, 5),
            "baseline_ref": {"pf": 1.7557, "wr": 50.77, "cvar5": -21.76, "mean_pnl": 2.343},
            "tgate300_ref": {"pf": 1.9141, "wr": 44.62, "cvar5": -14.53, "mean_pnl": 2.369},
        },
        "trades": records,
    }
    json.dump(out, open(OUT_DIR / "raw_trajectories.json", "w"), indent=1)
    print(f"  wrote raw_trajectories.json  (n={len(records)}, max reconciliation err={max_recon_err:.5f}%)")
    # sanity
    winners = [r for r in records if r["is_winner"]]
    losers = [r for r in records if not r["is_winner"]]
    print(f"  winners={len(winners)} losers={len(losers)}")
    print(f"  spread coverage: {sum(1 for r in records if r['entry_spread_pct'] is not None)}/{len(records)}")


if __name__ == "__main__":
    main()
