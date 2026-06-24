"""Phase LULD-V3c — T5a re-validation sweep (corrected anchor + normalized liq penalty).

Re-runs the V3b-T6-equivalent duration sweep on the same 100-event val sample and the
same halt-detection logic, but with the two confirmed T5a fixes in place:

  T3 fix  the labeler now records ``limit_state_start`` (seg_start) and the scorer
          matches fires against the limit-state window [onset - 15s, seg_end] rather
          than only the 15s before seg_end.
  T4 fix  the liquidity penalty is normalized to [0, 1] (spread_bps / TARGET_SPREAD_BPS).

The halt *set* is unchanged from V3b T5 (same detection); only the onset field is added,
so re-labeling here is the additive regeneration the T3 audit justified.

Writes:
  results/phase_luld_v3c/halt_labels_v3c.json
  results/phase_luld_v3c/t5a_dur{d}_score.json
  results/phase_luld_v3c/t5a_corrected_sweep.md   (before/after vs V3b T6)

Usage
-----
python scripts/luld_v3c_t5a.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ for sibling import

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from core.features.luld_halt_detection import detect_luld_halts
from core.exits.luld_scoring import FireEvent, HaltLabel, aggregate_scores, score_fires
from data.loaders.trades import load_trades
from data.loaders.quotes import load_quotes

# Reuse the fast vectorized replay from the (fixed) V3b T6 script.
from luld_v3b_t6 import _replay_all_durations  # noqa: E402

V3_DIR = _HERE / "results" / "phase_luld_v3"
V3C_DIR = _HERE / "results" / "phase_luld_v3c"

BAND_TIER = "tier2"
LIMIT_STATE_SECONDS = 15
HALT_GAP_SECONDS = 300

PROXIMITY_THRESHOLD = 0.010
PRE_HALT_WINDOW_SEC = 15.0
W_RECALL, W_FP, W_LIQ = 3.0, 1.0, 1.0
POSITION_VALUE_USD = 1000.0
DURATION_SWEEP_SEC = [0, 2, 4, 6, 8, 10, 12]

# V3b T6 baseline (raw-bps penalty, seg_end anchor) for the before/after table.
V3B_T6 = {
    0:  dict(n_fires=1387, tp=0, fp=1387, fn=32, recall=0.0000, fp_rate=1.0000, liq=44.750, comp=-38.9976),
    2:  dict(n_fires=256,  tp=1, fp=255,  fn=31, recall=0.0312, fp_rate=0.9961, liq=41.174, comp=-13.4472),
    4:  dict(n_fires=171,  tp=1, fp=170,  fn=31, recall=0.0312, fp_rate=0.9942, liq=43.162, comp=-15.2834),
    6:  dict(n_fires=142,  tp=2, fp=140,  fn=30, recall=0.0625, fp_rate=0.9859, liq=34.589, comp=-13.5699),
    8:  dict(n_fires=113,  tp=2, fp=111,  fn=30, recall=0.0625, fp_rate=0.9823, liq=42.370, comp=-14.7141),
    10: dict(n_fires=96,   tp=2, fp=94,   fn=30, recall=0.0625, fp_rate=0.9792, liq=45.431, comp=-11.9252),
    12: dict(n_fires=84,   tp=4, fp=80,   fn=28, recall=0.1250, fp_rate=0.9524, liq=41.243, comp=-10.1385),
}

HS_MIN_RECALL = 0.70
HS_MAX_LIQ = 0.5


def _load_trades_df(ticker: str, date: str) -> pd.DataFrame:
    from data.schemas.mom_db import FILTERED_DIR
    cand = list(FILTERED_DIR.glob(f"{ticker}_{date}_*"))
    if not cand:
        return pd.DataFrame()
    path = cand[0] / "trades.parquet"
    if not path.exists():
        return pd.DataFrame()
    t = pq.read_table(str(path), columns=["sip_timestamp", "price", "size"]).to_pandas()
    t["sip_timestamp"] = pd.to_datetime(t["sip_timestamp"], unit="ns")
    return t.sort_values("sip_timestamp").set_index("sip_timestamp")


def relabel(events: list) -> dict:
    print("Re-labeling (additive: + limit_state_start) ...", flush=True)
    rows = []
    total = 0
    with_h = 0
    for ev in events:
        tkr, date = ev["ticker"], ev["date"]
        if ev.get("skipped"):
            rows.append({"ticker": tkr, "date": date, "n_halts": 0, "halts": [], "skipped": True})
            continue
        df = _load_trades_df(tkr, date)
        if df.empty:
            rows.append({"ticker": tkr, "date": date, "n_halts": 0, "halts": [], "skipped": True})
            continue
        halts = detect_luld_halts(
            df, price_col="price", size_col="size", band_tier=BAND_TIER,
            limit_state_seconds=LIMIT_STATE_SECONDS, halt_gap_seconds=HALT_GAP_SECONDS,
        )
        hl = []
        for h in halts:
            lss = float(h.limit_state_start.timestamp()) if h.limit_state_start is not None else None
            hl.append({
                "start_sec": float(h.start.timestamp()),
                "end_sec": float(h.end.timestamp()),
                "limit_state_start_sec": lss,
                "reason": h.reason,
            })
        if hl:
            with_h += 1
            total += len(hl)
        rows.append({"ticker": tkr, "date": date, "n_halts": len(hl), "halts": hl, "skipped": False})
    out = {
        "meta": {"labeler_version": "v3c", "total_halts": total, "n_events_with_halts": with_h},
        "events": rows,
    }
    (V3C_DIR / "halt_labels_v3c.json").write_text(json.dumps(out, indent=2))
    print(f"  total halts: {total} across {with_h} events", flush=True)
    return out


def run_sweep(events, label_data, durations) -> List[dict]:
    n = len(durations)
    per_scores: List[list] = [[] for _ in durations]
    skipped = [0] * n
    lab_by_key = {(e["ticker"], e["date"]): e for e in label_data["events"]}

    for i, ev in enumerate(events):
        tkr, date = ev["ticker"], ev["date"]
        lab = lab_by_key.get((tkr, date), {})
        if ev.get("skipped") or lab.get("skipped"):
            for k in range(n):
                skipped[k] += 1
            continue
        halt_labels = [
            HaltLabel(start_sec=h["start_sec"], end_sec=h["end_sec"],
                      reason=h["reason"], limit_state_start_sec=h.get("limit_state_start_sec"))
            for h in lab.get("halts", [])
        ]
        try:
            td = load_trades(tkr, date, 0.0)
            if td is None or td.n_trades < 30:
                for k in range(n):
                    skipped[k] += 1
                continue
            qd = load_quotes(tkr, date, 0.0)
            if qd is None or qd.n_quotes < 10:
                for k in range(n):
                    skipped[k] += 1
                continue
        except Exception as exc:
            print(f"  LOAD ERROR {tkr} {date}: {exc}", flush=True)
            for k in range(n):
                skipped[k] += 1
            continue

        fires_per_dur = _replay_all_durations(td, qd, [float(d) for d in durations])
        for k, fires in enumerate(fires_per_dur):
            per_scores[k].append(score_fires(
                fires=fires, halts=halt_labels, pre_halt_window_sec=PRE_HALT_WINDOW_SEC,
                w_recall=W_RECALL, w_fp=W_FP, w_liq=W_LIQ, position_value_usd=POSITION_VALUE_USD,
            ))
        if (i + 1) % 20 == 0:
            print(f"  [{i+1}/{len(events)}]", flush=True)

    results = []
    for k, dur in enumerate(durations):
        agg = aggregate_scores(per_scores[k])
        out = {
            "config": {"luld_exit_duration_sec": dur, "proximity_threshold": PROXIMITY_THRESHOLD,
                       "pre_halt_window_sec": PRE_HALT_WINDOW_SEC, "labeler_version": "v3c"},
            "aggregate": {
                "n_fires": agg.n_fires, "n_halts": agg.n_halts, "tp": agg.tp, "fp": agg.fp,
                "fn": agg.fn, "recall": round(agg.recall, 4), "precision": round(agg.precision, 4),
                "fp_rate": round(agg.fp_rate, 4), "mean_liq_penalty": round(agg.mean_liq_penalty, 4),
                "composite": round(agg.composite, 4),
            },
        }
        (V3C_DIR / f"t5a_dur{int(dur)}_score.json").write_text(json.dumps(out, indent=2))
        a = out["aggregate"]
        print(f"  dur={dur:2d} | fires={a['n_fires']:4d} | tp={a['tp']:2d} fp={a['fp']:4d} fn={a['fn']:2d} "
              f"| recall={a['recall']:.4f} fp_rate={a['fp_rate']:.4f} | liq={a['mean_liq_penalty']:.4f} "
              f"| comp={a['composite']:+.4f}", flush=True)
        results.append(out)
    return results


def build_md(results, total_halts) -> str:
    L = []
    L.append("# Phase LULD-V3c — T5a Corrected Sweep (before/after vs V3b T6)")
    L.append("")
    L.append("**Date:** 2026-06-20")
    L.append("**Fixes applied:** T3 limit-state-window anchor + T4 normalized liquidity penalty "
             f"(TARGET_SPREAD_BPS=100). Weights unchanged ({W_RECALL}/{W_FP}/{W_LIQ}).")
    L.append(f"**Total halts (unchanged from V3b T5):** {total_halts}")
    L.append("")

    max_recall = max(r["aggregate"]["recall"] for r in results)
    min_liq = min(r["aggregate"]["mean_liq_penalty"] for r in results)
    hs = []
    if max_recall < HS_MIN_RECALL:
        hs.append(f"max_recall={max_recall:.4f} < {HS_MIN_RECALL}")
    if min_liq > HS_MAX_LIQ:
        hs.append(f"min_mean_liq_penalty={min_liq:.4f} > {HS_MAX_LIQ}")
    if hs:
        L.append("## *** HARD STOP (T5a re-validation) ***")
        L.append("")
        for h in hs:
            L.append(f"  - {h}")
        L.append("")
        L.append("Per spec: audit missed something OR the signal is genuine. Do NOT proceed to "
                 "Part B. Report to Cooper.")
        L.append("")
    else:
        L.append("## T5a Re-validation: PASS (recall and liquidity penalty in believable range)")
        L.append("")

    L.append("## Corrected sweep (V3c scorer)")
    L.append("")
    L.append("| dur | n_fires | tp | fp | fn | recall | fp_rate | mean_liq_pen | composite |")
    L.append("|----:|--------:|---:|---:|---:|-------:|--------:|-------------:|----------:|")
    for r in results:
        a = r["aggregate"]
        L.append(f"| {r['config']['luld_exit_duration_sec']:3d} | {a['n_fires']:7d} | {a['tp']:2d} "
                 f"| {a['fp']:4d} | {a['fn']:2d} | {a['recall']:.4f} | {a['fp_rate']:.4f} "
                 f"| {a['mean_liq_penalty']:12.4f} | {a['composite']:+9.4f} |")
    L.append("")
    L.append("## Before/after (recall & TP)")
    L.append("")
    L.append("| dur | V3b recall | V3c recall | V3b tp | V3c tp | V3b liq(bps) | V3c liq(norm) |")
    L.append("|----:|-----------:|-----------:|-------:|-------:|-------------:|--------------:|")
    for r in results:
        d = r["config"]["luld_exit_duration_sec"]
        a = r["aggregate"]
        b = V3B_T6.get(d)
        if b:
            L.append(f"| {d:3d} | {b['recall']:.4f} | {a['recall']:.4f} | {b['tp']:2d} | {a['tp']:2d} "
                     f"| {b['liq']:.3f} | {a['mean_liq_penalty']:.4f} |")
    L.append("")
    L.append("---")
    L.append("")
    L.append("**Cooper reviews before any Part B (liquidity-adaptive tiers) or follow-on phase.**")
    return "\n".join(L)


def main() -> None:
    V3C_DIR.mkdir(parents=True, exist_ok=True)
    v3 = json.load(open(V3_DIR / "halt_labels.json"))
    events = v3["events"]
    print(f"Loaded {len(events)} events (seed=42, val)")
    labels = relabel(events)
    total_halts = labels["meta"]["total_halts"]
    print(f"\nSweep durations {DURATION_SWEEP_SEC} (corrected scorer)")
    results = run_sweep(events, labels, DURATION_SWEEP_SEC)
    md = build_md(results, total_halts)
    (V3C_DIR / "t5a_corrected_sweep.md").write_text(md, encoding="utf-8")
    print("\n" + md)


if __name__ == "__main__":
    main()
