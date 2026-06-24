"""Phase LULD-V3c — FN diagnostic for the T5a recall hard-stop.

T5a cleared the liquidity hard-stop but recall plateaued at 0.25 (8/32 halts), below
the 0.70 threshold. This script answers the binding question for Cooper: are the 24 FN
halts FN because the pre-onset lead window (15s) is too narrow to catch approach fires
(a metric-design choice), or because no fire occurs anywhere near them (signal limit)?

For a fixed duration it replays all events once, then re-scores recall/fp_rate at a range
of pre-onset lead widths. It also reports, per FN halt, the nearest fire's signed offset
to the limit-state window so genuine no-fire halts are visible.

Usage
-----
python scripts/luld_v3c_fn_diag.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

from core.exits.luld_scoring import FireEvent, HaltLabel, aggregate_scores, score_fires
from data.loaders.trades import load_trades
from data.loaders.quotes import load_quotes
from luld_v3b_t6 import _replay_all_durations

V3C_DIR = _HERE / "results" / "phase_luld_v3c"
DUR = 6.0
LEAD_WIDTHS = [15.0, 30.0, 60.0, 120.0, 300.0]


def main() -> None:
    labels = json.load(open(V3C_DIR / "halt_labels_v3c.json"))
    lab_by_key = {(e["ticker"], e["date"]): e for e in labels["events"]}

    # Replay once per event, cache fires.
    cache = {}
    for ev in labels["events"]:
        tkr, date = ev["ticker"], ev["date"]
        if ev.get("skipped") or ev["n_halts"] == 0 and ev.get("skipped"):
            pass
        try:
            td = load_trades(tkr, date, 0.0)
            qd = load_quotes(tkr, date, 0.0)
        except Exception:
            continue
        if td is None or td.n_trades < 30 or qd is None or qd.n_quotes < 10:
            continue
        fires = _replay_all_durations(td, qd, [DUR])[0]
        cache[(tkr, date)] = fires

    # Recall / fp_rate sweep over pre-onset lead width.
    print(f"Recall vs pre-onset lead width (dur={DUR:.0f}s, 32 halts)\n")
    print(f"  {'lead_s':>7s} {'tp':>3s} {'fn':>3s} {'recall':>7s} {'fp':>5s} {'fp_rate':>8s}")
    for W in LEAD_WIDTHS:
        scores = []
        for key, ev in lab_by_key.items():
            if ev.get("skipped"):
                continue
            fires = cache.get(key, [])
            halt_labels = [
                HaltLabel(start_sec=h["start_sec"], end_sec=h["end_sec"],
                          reason=h["reason"], limit_state_start_sec=h.get("limit_state_start_sec"))
                for h in ev.get("halts", [])
            ]
            if not fires and not halt_labels:
                continue
            scores.append(score_fires(fires=fires, halts=halt_labels, pre_halt_window_sec=W))
        agg = aggregate_scores(scores)
        print(f"  {W:>7.0f} {agg.tp:>3d} {agg.fn:>3d} {agg.recall:>7.4f} {agg.fp:>5d} {agg.fp_rate:>8.4f}")

    # Per-halt nearest-fire offset (at the widest window, to expose genuine no-fire halts).
    print("\nPer-halt nearest-fire offset to limit-state window [onset, seg_end] (dur=6):")
    print(f"  {'ticker':6s} {'onset_off':>9s} {'note':<28s}")
    genuine_nofire = 0
    for key, ev in lab_by_key.items():
        if ev.get("skipped") or ev["n_halts"] == 0:
            continue
        fires = cache.get(key, [])
        fire_ts = np.array([f.timestamp_ns / 1e9 for f in fires]) if fires else np.array([])
        for h in ev["halts"]:
            onset = h.get("limit_state_start_sec") or h["start_sec"]
            seg_end = h["start_sec"]
            if len(fire_ts) == 0:
                print(f"  {key[0]:6s} {'--':>9s} no fires in entire event")
                genuine_nofire += 1
                continue
            # nearest fire to the window; offset relative to onset (neg = before onset)
            in_win = (fire_ts >= onset) & (fire_ts <= seg_end)
            if in_win.any():
                continue  # caught at lead>=0
            # nearest fire outside the window
            offs = fire_ts - onset
            nearest = offs[np.argmin(np.abs(offs))]
            if nearest < 0:
                note = f"approach fire {-nearest:.0f}s before onset"
            else:
                note = f"fire {nearest:.0f}s after seg_end"
            if abs(nearest) > 300:
                note += " (genuine no-fire near halt)"
                genuine_nofire += 1
            print(f"  {key[0]:6s} {nearest:>9.0f} {note:<28s}")

    print(f"\n  Halts with no fire within 300s of the window: {genuine_nofire}")


if __name__ == "__main__":
    main()
