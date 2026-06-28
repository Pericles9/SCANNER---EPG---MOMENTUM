"""
Phase R3 T2 — compute roc_5m per event for val_r3_stratified, using RocBuffer (C4).

roc_5m = pct_change[hit] - pct_change[ref], pct_change = (price - prev_close)/prev_close.
The poll history is simulated from tick data at a 20s cadence (within the live 15-30s band):
each poll t carries the last tick price at/before t. RocBuffer.compute then picks the
reference poll (most recent >=300s old; oldest if none old enough = partial; None = first
appearance — no prior poll, i.e. scanner hit at the first trade / pre-market gap-up).

Writes phase_r3/roc_values.json (one record per event).
"""
from __future__ import annotations
import json
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

BACKTEST = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKTEST))

from data.loaders.trades import load_trades
from core.filters.roc_buffer import RocBuffer

SAMPLE = BACKTEST / "data" / "val_r3_stratified.json"
OUT = BACKTEST / "results" / "phase_r3" / "roc_values.json"
OUT.parent.mkdir(parents=True, exist_ok=True)
POLL_NS = 20 * 1_000_000_000          # 20s poll cadence
WINDOW_SEC = 300.0
RETENTION_GUARD_NS = 660 * 1_000_000_000


def one(rec):
    tk, dt = rec["ticker"], rec["date"]
    try:
        td = load_trades(tk, dt, rec["mom_pct"])
        if td is None or td.n_trades < 1:
            return {"ticker": tk, "date": dt, "error": "no_ticks"}
        ts = td.timestamps.astype(np.int64)
        px = td.prices.astype(np.float64)
        pc = float(rec["prev_close"])
        hit_ts = int(rec["scanner_hit_ts_ns"])
        hit_px = float(rec["scanner_hit_price"])
        first_ts = int(ts[0])

        buf = RocBuffer(window_sec=WINDOW_SEC)
        grid_start = max(first_ts, hit_ts - RETENTION_GUARD_NS)
        # feed simulated polls up to (not including) the hit
        poll = grid_start
        while poll < hit_ts:
            j = int(np.searchsorted(ts, poll, side="right")) - 1
            if j >= 0:
                buf.update(tk, int(poll), (float(px[j]) - pc) / pc)
            poll += POLL_NS
        # final poll at the scanner hit
        buf.update(tk, hit_ts, (hit_px - pc) / pc)
        roc, window = buf.compute(tk, hit_ts)

        is_first = roc is None
        is_partial = (roc is not None) and (window < WINDOW_SEC)
        return {
            "ticker": tk, "date": dt, "stratum": rec["stratum"],
            "gap_pct_at_hit": rec["gap_pct_at_hit"],
            "scanner_hit_idx": rec["scanner_hit_idx"],
            "scanner_roc_5m_at_fire": (round(float(roc), 6) if roc is not None else None),
            "scanner_roc_window_sec_actual": (round(float(window), 2) if roc is not None else None),
            "is_first_appearance": bool(is_first),
            "is_partial_window": bool(is_partial),
        }
    except Exception as e:
        return {"ticker": tk, "date": dt, "error": str(e)}


def main():
    events = json.load(open(SAMPLE))["events"]
    print(f"computing roc_5m for {len(events)} events (6 workers)...")
    out = []
    with ProcessPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(one, e): e for e in events}
        for fu in as_completed(futs):
            out.append(fu.result())
    errs = [r for r in out if "error" in r]
    if errs:
        print(f"ERRORS ({len(errs)}): {errs[:5]}")
    out.sort(key=lambda r: (r.get("stratum", ""), r["ticker"], r["date"]))
    OUT.write_text(json.dumps(out, indent=2))

    vals = [r["scanner_roc_5m_at_fire"] for r in out if r.get("scanner_roc_5m_at_fire") is not None]
    n_first = sum(1 for r in out if r.get("is_first_appearance"))
    n_partial = sum(1 for r in out if r.get("is_partial_window"))
    n_full = len(vals) - n_partial
    a = np.array(vals)
    print(f"\nT2a: N_total={len(out)}  full_window={n_full}  partial={n_partial}  first_appearance={n_first}")
    if len(a):
        ps = {p: round(float(np.percentile(a, p)), 4) for p in (0, 10, 25, 50, 75, 90, 100)}
        print(f"T2b: roc_5m dist (n={len(a)}): min={ps[0]} p10={ps[10]} p25={ps[25]} "
              f"median={ps[50]} p75={ps[75]} p90={ps[90]} max={ps[100]}")
        below05 = int(np.sum(a < 0.05))
        print(f"      frac roc_5m<0.05: {below05}/{len(a)} = {100*below05/len(a):.1f}%  "
              f"(escalation flag if >50% of ALL events)")
        print(f"      frac of ALL events with roc<0.05: {100*below05/len(out):.1f}%")
    print(f"written: {OUT}")


if __name__ == "__main__":
    main()
