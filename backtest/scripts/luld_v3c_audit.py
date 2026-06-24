"""Phase LULD-V3c — Audit evidence generator (T2/T3/T4).

Produces hard numerical evidence for the three code audits, in one pass over the
18 T5-labeled halt events:

  T2  pin detection / duration clock:  per-event pin-state trace around each
      limit-state segment (flicker check) + tolerance-width occupancy.
  T3  matching / timestamp anchor:  for every fire, lead time to BOTH the
      current scoring anchor (seg_end == HaltLabel.start_sec) and the proposed
      anchor (seg_start == limit-state onset). Shows how many fires are scored
      FP only because the anchor sits at the wrong end of the segment.
  T4  liquidity penalty units:  10-fire intermediate-value table (spread_bps,
      bid_size, shares_needed, mid, raw penalty, normalized penalty).

Read-only: imports the production modules, does not modify them.

Usage
-----
python scripts/luld_v3c_audit.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import numpy as np
import pandas as pd

from core.exits.luld_proximity import LuldProximityExit, ProximityState
from data.loaders.trades import load_trades
from data.loaders.quotes import load_quotes

V3B_DIR = _HERE / "results" / "phase_luld_v3b"
V3C_DIR = _HERE / "results" / "phase_luld_v3c"

# Locked params (match V3b T6)
PROXIMITY_THRESHOLD = 0.010
REF_WINDOW_SEC = 300.0
WARMUP_SEC = 60.0
PRE_HALT_WINDOW_SEC = 15.0
POSITION_VALUE_USD = 1000.0

_ET = ZoneInfo("America/New_York")
_RTH_START = 9 * 3600 + 30 * 60
_DOUBLED_AM = 9 * 3600 + 45 * 60
_DOUBLED_PM = 15 * 3600 + 35 * 60
_RTH_END = 16 * 3600
_NS = 1_000_000_000


def _et_sec_of_day(ts_ns: np.ndarray) -> np.ndarray:
    ts_et = pd.to_datetime(ts_ns, unit="ns", utc=True).tz_convert(_ET)
    return (ts_et.hour * 3600 + ts_et.minute * 60 + ts_et.second).to_numpy(np.int32)


def _band_arrays(ts_ns: np.ndarray, prices: np.ndarray):
    """Replicate labeler + exit band: 5-min mean + 1% sticky, tier2 w/ TOD doubling.

    Returns (ref_published, upper_band, band_pct, et_sec) over the RTH subset,
    plus the RTH mask into the original arrays.
    """
    et_sec = _et_sec_of_day(ts_ns)
    rth = (et_sec >= _RTH_START) & (et_sec < _RTH_END)
    ts_r = ts_ns[rth]
    px_r = prices[rth]
    et_r = et_sec[rth]
    if len(ts_r) == 0:
        return None

    band_pct = np.where((et_r < _DOUBLED_AM) | (et_r >= _DOUBLED_PM), 0.20, 0.10)
    ser = pd.Series(px_r, index=pd.to_datetime(ts_r, unit="ns"))
    ref_raw = ser.rolling("300s", min_periods=1).mean().to_numpy(np.float64)

    warmup_ns = int(WARMUP_SEC * _NS)
    in_warmup = (ts_r - ts_r[0]) < warmup_ns
    ref_pub = np.zeros(len(ts_r), np.float64)
    published = 0.0
    for i in range(len(ts_r)):
        if in_warmup[i] and published == 0.0:
            continue
        raw = ref_raw[i]
        if published == 0.0:
            published = raw
        elif published > 0 and abs(raw - published) / published >= 0.01:
            published = raw
        ref_pub[i] = published
    upper = np.round(ref_pub * (1.0 + band_pct), 2)
    return ts_r, px_r, et_r, ref_pub, upper, band_pct, rth


def _find_segments(ts_r, px_r, upper, ref_pub, min_state_sec=15) -> List[Tuple[int, int]]:
    """Find continuous price>=upper_band segments lasting >= min_state_sec.

    Returns list of (start_idx, end_idx) into the RTH arrays. Mirrors the
    labeler's limit_mask + limit_state_seconds gate (upper band only here).
    """
    in_band = (ref_pub > 0) & (upper > 0) & (px_r >= upper)
    segs = []
    start = None
    for i, b in enumerate(in_band):
        if b and start is None:
            start = i
        if not b and start is not None:
            end = i - 1
            if (ts_r[end] - ts_r[start]) / _NS >= min_state_sec:
                segs.append((start, end))
            start = None
    if start is not None:
        end = len(in_band) - 1
        if (ts_r[end] - ts_r[start]) / _NS >= min_state_sec:
            segs.append((start, end))
    return segs


def _align_quotes(ts_r, qd):
    n = len(ts_r)
    bid = np.zeros(n); ask = np.zeros(n); bsz = np.zeros(n)
    if qd is not None and qd.n_quotes > 0:
        q_ts = np.asarray(qd.timestamps[:qd.n_quotes], np.int64)
        q_idx = np.searchsorted(q_ts, ts_r, side="right") - 1
        valid = q_idx >= 0
        bid[valid] = np.asarray(qd.bid_prices[:qd.n_quotes], np.float64)[q_idx[valid]]
        ask[valid] = np.asarray(qd.ask_prices[:qd.n_quotes], np.float64)[q_idx[valid]]
        bsz[valid] = np.asarray(qd.bid_sizes[:qd.n_quotes], np.float64)[q_idx[valid]]
    return bid, ask, bsz


def _replay_fires(ts_r, px_r, bid, ask, bsz, upper, ref_pub, band_pct, et_r, dur):
    """Vectorized replay matching LuldProximityExit pin/clock logic over RTH.

    Returns (fires, pin_active) where fires is a list of dicts
    (idx, ts_ns, spread_bps, bid_size, mid, pin_dur) and pin_active is the
    per-tick boolean pin state. Replicates the module: pin active when the
    comparison price (bid when valid, else trade price) is within
    proximity_threshold of the upper band; EXIT_HALT fires once the pin has been
    sustained for >= dur seconds (clock resets when the pin breaks).
    """
    valid_q = (bid > 0) & (ask > bid)
    compare_px = np.where(valid_q, bid, px_r)
    pin_active = (
        (upper > 0) & (ref_pub > 0) & (compare_px > 0) &
        (compare_px >= upper * (1.0 - PROXIMITY_THRESHOLD))
    )
    ts_sec = ts_r / 1e9

    # Zone (contiguous pin runs) → fire at first tick where pin held >= dur
    diff = np.diff(pin_active.astype(np.int8), prepend=0, append=0)
    starts = np.where(diff == 1)[0]
    ends = np.where(diff == -1)[0]

    fires = []
    for z0, z1 in zip(starts, ends):
        entry = ts_sec[z0]
        target = entry + float(dur)
        zone_ts = ts_sec[z0:z1]
        pos = int(np.searchsorted(zone_ts, target))
        if pos >= len(zone_ts):
            continue
        i = z0 + pos
        if valid_q[i]:
            mid = (bid[i] + ask[i]) / 2.0
            spread_bps = (ask[i] - bid[i]) / mid * 1e4
            eff_bsz = float(bsz[i])
        elif px_r[i] > 0:
            mid = float(px_r[i]); spread_bps = 0.0; eff_bsz = 0.0
        else:
            continue
        fires.append({
            "idx": int(i), "ts_ns": int(ts_r[i]), "spread_bps": float(spread_bps),
            "bid_size": eff_bsz, "mid": float(mid),
            "pin_dur": float(ts_sec[i] - entry),
        })
    return fires, pin_active


def main() -> None:
    V3C_DIR.mkdir(parents=True, exist_ok=True)
    labels = json.load(open(V3B_DIR / "halt_labels.json"))
    halt_events = [e for e in labels["events"] if e["n_halts"] > 0]
    print(f"Auditing {len(halt_events)} halt events\n")

    t3_rows = []      # anchor comparison rows (per fire, dur=0 and dur=12)
    liq_rows = []     # liquidity intermediate values
    flicker_summary = []  # per-event pin flicker stats

    for ev in halt_events:
        tkr, date = ev["ticker"], ev["date"]
        try:
            td = load_trades(tkr, date, 0.0)
            qd = load_quotes(tkr, date, 0.0)
        except Exception as exc:
            print(f"  LOAD ERROR {tkr} {date}: {exc}")
            continue
        if td is None or td.n_trades < 30:
            continue
        ts_ns = np.asarray(td.timestamps[:td.n_trades], np.int64)
        prices = np.asarray(td.prices[:td.n_trades], np.float64)
        band = _band_arrays(ts_ns, prices)
        if band is None:
            continue
        ts_r, px_r, et_r, ref_pub, upper, band_pct, rth = band
        bid, ask, bsz = _align_quotes(ts_r, qd)
        segs = _find_segments(ts_r, px_r, upper, ref_pub)

        # Halt anchors from labels (seg_end == start_sec)
        halt_starts = [h["start_sec"] for h in ev["halts"]]

        for dur in (0.0, 12.0):
            fires, pin_active = _replay_fires(
                ts_r, px_r, bid, ask, bsz, upper, ref_pub, band_pct, et_r, dur
            )
            # T3: for each fire, lead to nearest seg_end (current) & seg_start (proposed)
            seg_ends_sec = [ts_r[e] / 1e9 for _, e in segs]
            seg_starts_sec = [ts_r[s] / 1e9 for s, _ in segs]
            for f in fires:
                fts = f["ts_ns"] / 1e9
                # current anchor: min nonneg lead to any halt_start (seg_end)
                cur_leads = [hs - fts for hs in halt_starts if hs - fts >= 0]
                cur_lead = min(cur_leads) if cur_leads else None
                # proposed anchor: min nonneg lead to any seg_start
                prop_leads = [ss - fts for ss in seg_starts_sec if ss - fts >= -1.0]
                prop_lead = min(prop_leads, key=abs) if prop_leads else None
                tp_cur = bool(cur_lead is not None and 0 <= cur_lead <= PRE_HALT_WINDOW_SEC)
                tp_prop = bool(prop_lead is not None and -1.0 <= prop_lead <= PRE_HALT_WINDOW_SEC)
                t3_rows.append({
                    "ticker": tkr, "date": date, "dur": float(dur), "fire_ts": round(float(fts), 2),
                    "lead_to_segend_cur": None if cur_lead is None else round(float(cur_lead), 1),
                    "lead_to_segstart_prop": None if prop_lead is None else round(float(prop_lead), 1),
                    "tp_current": tp_cur, "tp_proposed": tp_prop,
                })
            # T4: collect liquidity intermediates from dur=0 fires (most fires)
            if dur == 0.0:
                for f in fires[:3]:  # cap per event; we want ~10 total mix
                    shares_needed = POSITION_VALUE_USD / f["mid"] if f["mid"] > 0 else float("inf")
                    insufficient = bool(f["bid_size"] < shares_needed)
                    raw_pen = f["spread_bps"] if insufficient else 0.0
                    liq_rows.append({
                        "ticker": tkr, "date": date,
                        "spread_bps": round(float(f["spread_bps"]), 2),
                        "bid_size": round(float(f["bid_size"]), 0),
                        "mid": round(float(f["mid"]), 4),
                        "shares_needed": round(float(shares_needed), 1),
                        "insufficient": insufficient,
                        "raw_penalty": round(float(raw_pen), 2),
                    })
            # T2: flicker — count pin on/off transitions during longest segment
            if dur == 0.0 and segs:
                longest = max(segs, key=lambda se: se[1] - se[0])
                s0, s1 = longest
                lo = max(0, s0 - 20)
                hi = min(len(pin_active), s1 + 21)
                actives = pin_active[lo:hi]
                transitions = int(np.sum(actives[1:] != actives[:-1])) if len(actives) > 1 else 0
                seg_secs = (ts_r[s1] - ts_r[s0]) / 1e9
                flicker_summary.append({
                    "ticker": tkr, "date": date,
                    "seg_secs": round(float(seg_secs), 0),
                    "n_ticks_in_seg": int(s1 - s0 + 1),
                    "pin_transitions": transitions,
                    "pct_pinned": round(100 * float(np.mean(actives)), 1) if len(actives) else 0.0,
                })

    # ---- Write evidence ----
    out = {
        "t3_anchor_rows": t3_rows,
        "t4_liquidity_rows": liq_rows,
        "t2_flicker_summary": flicker_summary,
    }
    (V3C_DIR / "audit_evidence.json").write_text(json.dumps(out, indent=2))

    # T3 summary
    n_fires = len([r for r in t3_rows if r["dur"] == 0.0])
    tp_cur = sum(1 for r in t3_rows if r["dur"] == 0.0 and r["tp_current"])
    tp_prop = sum(1 for r in t3_rows if r["dur"] == 0.0 and r["tp_proposed"])
    print("=" * 64)
    print("T3 — ANCHOR MISMATCH (dur=0)")
    print("=" * 64)
    print(f"  total fires:                 {n_fires}")
    print(f"  TP under CURRENT anchor (seg_end):   {tp_cur}")
    print(f"  TP under PROPOSED anchor (seg_start): {tp_prop}")
    rescued = sum(1 for r in t3_rows if r["dur"] == 0.0 and r["tp_proposed"] and not r["tp_current"])
    print(f"  fires rescued by anchor fix:         {rescued}")
    print()
    print("  Sample fires (dur=0, first 15):")
    print(f"  {'ticker':6s} {'fire_ts':>14s} {'lead_segEND':>11s} {'lead_segSTART':>13s} {'TPcur':>6s} {'TPprop':>7s}")
    for r in [x for x in t3_rows if x["dur"] == 0.0][:15]:
        print(f"  {r['ticker']:6s} {r['fire_ts']:>14.1f} {str(r['lead_to_segend_cur']):>11s} "
              f"{str(r['lead_to_segstart_prop']):>13s} {str(r['tp_current']):>6s} {str(r['tp_proposed']):>7s}")

    print()
    print("=" * 64)
    print("T4 — LIQUIDITY PENALTY INTERMEDIATES (10 fires)")
    print("=" * 64)
    print(f"  {'ticker':6s} {'spread_bps':>10s} {'bid_size':>9s} {'mid':>9s} {'shares_nd':>10s} {'insuff':>7s} {'raw_pen':>8s}")
    for r in liq_rows[:10]:
        print(f"  {r['ticker']:6s} {r['spread_bps']:>10.2f} {r['bid_size']:>9.0f} {r['mid']:>9.4f} "
              f"{r['shares_needed']:>10.1f} {str(r['insufficient']):>7s} {r['raw_penalty']:>8.2f}")
    if liq_rows:
        raws = [r["raw_penalty"] for r in liq_rows]
        print(f"\n  raw_penalty range: [{min(raws):.2f}, {max(raws):.2f}]  mean={np.mean(raws):.2f}")
        print("  --> penalty is RAW spread_bps (basis points), NOT normalized to [0,1].")
        print("      Composite subtracts this directly from recall(max 3.0)/fp_rate(max 1.0).")

    print()
    print("=" * 64)
    print("T2 — PIN FLICKER (longest segment per event, dur=0)")
    print("=" * 64)
    print(f"  {'ticker':6s} {'seg_secs':>8s} {'ticks':>6s} {'transitions':>11s} {'pct_pinned':>10s}")
    for r in flicker_summary:
        print(f"  {r['ticker']:6s} {r['seg_secs']:>8.0f} {r['n_ticks_in_seg']:>6d} "
              f"{r['pin_transitions']:>11d} {r['pct_pinned']:>9.1f}%")

    print(f"\nEvidence written: {V3C_DIR / 'audit_evidence.json'}")


if __name__ == "__main__":
    main()
