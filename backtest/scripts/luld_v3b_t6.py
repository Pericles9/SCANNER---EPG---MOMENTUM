"""Phase LULD-V3b T6 — Re-label + confusion-matrix duration sweep.

Two-stage script:
  Stage 1: Re-run detect_luld_halts (T5 labeler: 5-min mean + 1% sticky)
           on the same 100-event val sample used in Phase LULD-V3.
           Writes: results/phase_luld_v3b/halt_labels.json

  Stage 2: For each duration in DURATION_SWEEP_SEC, replay LuldProximityExit
           through the full session and score confusion matrix.
           Writes: results/phase_luld_v3b/t6_dur{d}_score.json
                   results/phase_luld_v3b/t6_summary.md

Hard-stop criteria (per spec):
  - Total halt count <= 1
  - No config reaches recall >= 0.70
  - mean_liq_penalty > 0.5 everywhere
  - Fewer than 10 total halts

Usage
-----
python -m backtest.scripts.luld_v3b_t6
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List, Optional

_HERE = Path(__file__).resolve().parent.parent   # backtest/
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from core.features.luld_halt_detection import detect_luld_halts
from core.exits.luld_proximity import LuldProximityExit, ProximityState
from core.exits.luld_scoring import FireEvent, HaltLabel, aggregate_scores, score_fires
from data.loaders.trades import load_trades
from data.loaders.quotes import load_quotes

V3_RESULTS_DIR = _HERE / "results" / "phase_luld_v3"
V3B_RESULTS_DIR = _HERE / "results" / "phase_luld_v3b"

# Labeler params (T5 reconciled — must match LuldProximityExit)
BAND_TIER = "tier2"
LIMIT_STATE_SECONDS = 15
HALT_GAP_SECONDS = 300

# Replay / scoring params (locked from V3; do not change)
PROXIMITY_THRESHOLD = 0.010
REF_WINDOW_SEC = 300.0
WARMUP_SEC = 60.0
PRE_HALT_WINDOW_SEC = 15.0
W_RECALL = 3.0
W_FP = 1.0
W_LIQ = 1.0
POSITION_VALUE_USD = 1000.0

# Duration sweep
DURATION_SWEEP_SEC = [0, 2, 4, 6, 8, 10, 12]

# Hard-stop thresholds
HS_MIN_HALTS = 10
HS_MIN_RECALL = 0.70
HS_MAX_LIQ_PENALTY = 0.5


# ── Stage 1: Re-labeling ────────────────────────────────────────────────────


def _load_trades_df(ticker: str, date: str) -> pd.DataFrame:
    """Load trades.parquet as a DatetimeIndex DataFrame (tz-naive UTC)."""
    from data.schemas.mom_db import FILTERED_DIR
    candidates = list(FILTERED_DIR.glob(f"{ticker}_{date}_*"))
    if not candidates:
        return pd.DataFrame()
    path = candidates[0] / "trades.parquet"
    if not path.exists():
        return pd.DataFrame()
    table = pq.read_table(str(path), columns=["sip_timestamp", "price", "size"])
    df = table.to_pandas()
    df["sip_timestamp"] = pd.to_datetime(df["sip_timestamp"], unit="ns")
    df = df.sort_values("sip_timestamp").set_index("sip_timestamp")
    return df


def run_stage1(events: list[dict]) -> dict:
    """Re-label all events using T5 detect_luld_halts; return label dict."""
    print(f"\n{'='*60}")
    print("STAGE 1 — Re-labeling with T5 labeler (5-min mean + 1% sticky)")
    print(f"{'='*60}")

    event_rows = []
    total_halts = 0
    n_with_halts = 0
    n_skipped = 0

    for ev in events:
        ticker, date = ev["ticker"], ev["date"]
        if ev.get("skipped"):
            n_skipped += 1
            event_rows.append({
                "ticker": ticker, "date": date, "n_halts": 0,
                "halts": [], "skipped": True,
            })
            continue

        df = _load_trades_df(ticker, date)
        if df.empty:
            print(f"  SKIP {ticker} {date}: no trades.parquet")
            n_skipped += 1
            event_rows.append({
                "ticker": ticker, "date": date, "n_halts": 0,
                "halts": [], "skipped": True,
            })
            continue

        halts = detect_luld_halts(
            df,
            price_col="price",
            size_col="size",
            band_tier=BAND_TIER,
            limit_state_seconds=LIMIT_STATE_SECONDS,
            halt_gap_seconds=HALT_GAP_SECONDS,
        )

        halt_list = [
            {
                "start_sec": float(h.start.timestamp()),
                "end_sec": float(h.end.timestamp()),
                "reason": h.reason,
                "start_str": str(h.start),
                "end_str": str(h.end),
            }
            for h in halts
        ]
        if halt_list:
            n_with_halts += 1
            total_halts += len(halt_list)
            print(f"  {ticker} {date}: {len(halt_list)} halt(s) detected")

        event_rows.append({
            "ticker": ticker, "date": date,
            "n_halts": len(halt_list),
            "halts": halt_list,
            "skipped": False,
        })

    output = {
        "meta": {
            "band_tier": BAND_TIER,
            "limit_state_seconds": LIMIT_STATE_SECONDS,
            "halt_gap_seconds": HALT_GAP_SECONDS,
            "sample_events": len(events),
            "seed": 42,
            "split": "val",
            "labeler_version": "v3b_t5",
            "n_events_with_halts": n_with_halts,
            "n_events_skipped": n_skipped,
            "total_halts": total_halts,
        },
        "events": event_rows,
    }

    out_path = V3B_RESULTS_DIR / "halt_labels.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nStage 1 complete: {out_path}")
    print(f"  Events: {len(events)} total | {n_with_halts} with halts | {n_skipped} skipped")
    print(f"  Total halt windows: {total_halts}")
    return output


# ── Stage 2: Duration sweep (load-once-replay-N) ────────────────────────────


def _replay_all_durations(
    td,
    qd,
    durations: List[float],
) -> List[List[FireEvent]]:
    """Vectorized LULD proximity replay for N duration configs.

    Replaces the per-tick Python loop with pandas/numpy operations:
      1. Filter to RTH timestamps (09:30–16:00 ET) — eliminates pre/post-market
      2. Rolling 300s arithmetic mean via pandas (vectorized, C-speed)
      3. Sticky filter: single Python loop over RTH trades only
      4. Quote alignment via np.searchsorted (fast forward-fill)
      5. Zone detection: np.where on proximity boolean array
      6. Fire detection per duration: searchsorted on zone-start timestamps

    O(n_rth * n_dur) → effectively O(n_rth + n_zones * n_dur), ~100–1000x faster
    than the previous per-tick approach with deque sum + timezone conversion.
    """
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")

    _RTH_START    = 9 * 3600 + 30 * 60        # 09:30:00
    _DOUBLED_AM   = 9 * 3600 + 45 * 60        # 09:45:00
    _DOUBLED_PM   = 15 * 3600 + 35 * 60       # 15:35:00
    _RTH_END      = 16 * 3600                  # 16:00:00
    _NS_PER_SEC   = 1_000_000_000

    n_trades = td.n_trades
    if n_trades == 0:
        return [[] for _ in durations]

    # --- build trade arrays ---------------------------------------------------
    ts_ns   = np.asarray(td.timestamps[:n_trades], dtype=np.int64)
    prices  = np.asarray(td.prices[:n_trades],     dtype=np.float64)

    # Vectorized ET conversion (timezone-aware) → seconds-of-day
    ts_utc = pd.to_datetime(ts_ns, unit="ns", utc=True)
    ts_et  = ts_utc.tz_convert(_ET)
    et_sec = (ts_et.hour * 3600 + ts_et.minute * 60 + ts_et.second).to_numpy(dtype=np.int32)

    is_rth = (et_sec >= _RTH_START) & (et_sec < _RTH_END)
    if not is_rth.any():
        return [[] for _ in durations]

    # Filter to RTH
    rth_mask     = is_rth
    ts_ns_rth    = ts_ns[rth_mask]
    prices_rth   = prices[rth_mask]
    et_sec_rth   = et_sec[rth_mask]
    n_rth        = len(ts_ns_rth)

    # Band pct per RTH tick (vectorized)
    band_pct_rth = np.where(
        (et_sec_rth < _DOUBLED_AM) | (et_sec_rth >= _DOUBLED_PM),
        0.20,
        0.10,
    )

    # Rolling 5-min arithmetic mean (pandas C-speed, replaces deque sum per tick)
    rth_series  = pd.Series(prices_rth, index=pd.to_datetime(ts_ns_rth, unit="ns"))
    ref_raw_arr = rth_series.rolling("300s", min_periods=1).mean().to_numpy(dtype=np.float64)

    # Warmup mask: first WARMUP_SEC after first RTH tick
    warmup_ns    = int(WARMUP_SEC * _NS_PER_SEC)
    session_start = ts_ns_rth[0]
    in_warmup    = (ts_ns_rth - session_start) < warmup_ns

    # Sticky filter — single Python loop (O(n_rth), not O(n_rth * n_buffer))
    ref_published = np.zeros(n_rth, dtype=np.float64)
    published = 0.0
    for i in range(n_rth):
        if in_warmup[i] and published == 0.0:
            continue  # ref_published[i] stays 0.0
        raw = ref_raw_arr[i]
        if published == 0.0:
            published = raw
        else:
            rel = abs(raw - published) / published if published > 0.0 else 1.0
            if rel >= 0.01:
                published = raw
        ref_published[i] = published

    # Upper band (Tier 2): ref × (1 + band_pct), rounded to nearest penny
    upper_band = np.round(ref_published * (1.0 + band_pct_rth), 2)

    # --- quote alignment via searchsorted (forward-fill) ----------------------
    n_quotes = qd.n_quotes
    bid_arr    = np.zeros(n_rth, dtype=np.float64)
    ask_arr    = np.zeros(n_rth, dtype=np.float64)
    bid_sz_arr = np.zeros(n_rth, dtype=np.float64)

    if n_quotes > 0:
        q_ts  = np.asarray(qd.timestamps[:n_quotes],  dtype=np.int64)
        q_bid = np.asarray(qd.bid_prices[:n_quotes],  dtype=np.float64)
        q_ask = np.asarray(qd.ask_prices[:n_quotes],  dtype=np.float64)
        q_bsz = np.asarray(qd.bid_sizes[:n_quotes],   dtype=np.float64)

        # Index of most-recent quote at or before each trade timestamp
        q_idx  = np.searchsorted(q_ts, ts_ns_rth, side="right") - 1
        valid  = q_idx >= 0
        bid_arr[valid]    = q_bid[q_idx[valid]]
        ask_arr[valid]    = q_ask[q_idx[valid]]
        bid_sz_arr[valid] = q_bsz[q_idx[valid]]

    # --- proximity zone (vectorized) ------------------------------------------
    valid_quote = (bid_arr > 0.0) & (ask_arr > bid_arr)
    compare_px  = np.where(valid_quote, bid_arr, prices_rth)

    # Condition: compare_price >= upper_band * (1 - threshold)
    in_zone = (
        (upper_band > 0.0) &
        (ref_published > 0.0) &
        (compare_px > 0.0) &
        (compare_px >= upper_band * (1.0 - PROXIMITY_THRESHOLD))
    )

    ts_sec_rth = ts_ns_rth / 1e9

    # Zone transitions — O(n_rth) numpy, fast
    diff = np.diff(in_zone.astype(np.int8), prepend=0, append=0)
    zone_starts = np.where(diff == 1)[0]   # rising edges
    zone_ends   = np.where(diff == -1)[0]  # falling edges

    # --- per-duration fire detection (O(n_zones * n_dur)) ---------------------
    fires_per_dur: List[List[FireEvent]] = []
    for dur in durations:
        dur_f = float(dur)
        fires: List[FireEvent] = []

        for z_start, z_end in zip(zone_starts, zone_ends):
            entry_sec      = ts_sec_rth[z_start]
            fire_target    = entry_sec + dur_f
            zone_ts        = ts_sec_rth[z_start:z_end]

            fire_pos = int(np.searchsorted(zone_ts, fire_target))
            if fire_pos >= len(zone_ts):
                continue  # zone ended before duration elapsed

            idx  = z_start + fire_pos
            bid  = bid_arr[idx]
            ask  = ask_arr[idx]
            bsz  = bid_sz_arr[idx]
            px   = prices_rth[idx]

            if bid > 0.0 and ask > bid:
                mid        = (bid + ask) / 2.0
                spread_bps = (ask - bid) / mid * 10_000.0
                eff_bid_sz = bsz
            elif px > 0.0:
                mid        = px
                spread_bps = 0.0
                eff_bid_sz = 0.0
            else:
                continue

            fires.append(FireEvent(
                timestamp_ns=int(ts_ns_rth[idx]),
                spread_bps=spread_bps,
                bid_size_shares=eff_bid_sz,
                mid_price=mid,
            ))

        fires_per_dur.append(fires)

    return fires_per_dur


def run_duration_sweep(
    events: list[dict],
    label_data: dict,
    durations: List[float],
) -> List[dict]:
    """Load each event once, replay all N duration configs, collect per-config scores."""
    n_dur = len(durations)
    per_event_scores: List[List] = [[] for _ in durations]
    per_event_detail: List[List] = [[] for _ in durations]
    n_skipped = [0] * n_dur
    n_errors = [0] * n_dur

    label_by_key = {
        (ev["ticker"], ev["date"]): ev
        for ev in label_data["events"]
    }

    for i, ev in enumerate(events):
        ticker, date = ev["ticker"], ev["date"]
        lab_ev = label_by_key.get((ticker, date), {})

        if ev.get("skipped") or lab_ev.get("skipped"):
            for k in range(n_dur):
                n_skipped[k] += 1
            continue

        halt_labels = [
            HaltLabel(start_sec=h["start_sec"], end_sec=h["end_sec"], reason=h["reason"])
            for h in lab_ev.get("halts", [])
        ]

        try:
            td = load_trades(ticker, date, 0.0)
            if td is None or td.n_trades < 30:
                for k in range(n_dur):
                    n_skipped[k] += 1
                continue
            qd = load_quotes(ticker, date, 0.0)
            if qd is None or qd.n_quotes < 10:
                for k in range(n_dur):
                    n_skipped[k] += 1
                continue
        except Exception as exc:
            print(f"  LOAD ERROR {ticker} {date}: {exc}")
            for k in range(n_dur):
                n_errors[k] += 1
            continue

        fires_per_dur = _replay_all_durations(td, qd, durations)

        for k, fires in enumerate(fires_per_dur):
            es = score_fires(
                fires=fires,
                halts=halt_labels,
                pre_halt_window_sec=PRE_HALT_WINDOW_SEC,
                w_recall=W_RECALL,
                w_fp=W_FP,
                w_liq=W_LIQ,
                position_value_usd=POSITION_VALUE_USD,
            )
            per_event_scores[k].append(es)
            per_event_detail[k].append({
                "ticker": ticker,
                "date": date,
                "n_fires": es.n_fires,
                "n_halts": es.n_halts,
                "tp": es.tp,
                "fp": es.fp,
                "fn": es.fn,
                "recall": round(es.recall, 4),
                "precision": round(es.precision, 4),
                "fp_rate": round(es.fp_rate, 4),
                "mean_liq_penalty": round(es.mean_liq_penalty, 4),
                "composite": round(es.composite, 4),
            })

        if (i + 1) % 10 == 0 or i == 0:
            print(f"  [{i+1}/100] events processed", flush=True)

    results = []
    for k, dur in enumerate(durations):
        agg = aggregate_scores(per_event_scores[k])
        output = {
            "config": {
                "proximity_threshold": PROXIMITY_THRESHOLD,
                "luld_exit_duration_sec": dur,
                "ref_window_sec": REF_WINDOW_SEC,
                "warmup_sec": WARMUP_SEC,
                "pre_halt_window_sec": PRE_HALT_WINDOW_SEC,
                "w_recall": W_RECALL,
                "w_fp": W_FP,
                "w_liq": W_LIQ,
                "position_value_usd": POSITION_VALUE_USD,
                "labeler_version": "v3b_t5",
            },
            "aggregate": {
                "n_events_scored": len(per_event_scores[k]),
                "n_events_skipped": n_skipped[k],
                "n_events_errored": n_errors[k],
                "n_fires": agg.n_fires,
                "n_halts": agg.n_halts,
                "tp": agg.tp,
                "fp": agg.fp,
                "fn": agg.fn,
                "recall": round(agg.recall, 4),
                "precision": round(agg.precision, 4),
                "fp_rate": round(agg.fp_rate, 4),
                "mean_liq_penalty": round(agg.mean_liq_penalty, 4),
                "composite": round(agg.composite, 4),
            },
            "per_event": per_event_detail[k],
        }
        tag = f"t6_dur{int(dur)}"
        out_path = V3B_RESULTS_DIR / f"{tag}_score.json"
        with open(out_path, "w") as f:
            json.dump(output, f, indent=2)

        a = output["aggregate"]
        print(
            f"  dur={dur:2.0f}s | fires={a['n_fires']:4d} | halts={a['n_halts']} | "
            f"tp={a['tp']} fp={a['fp']} fn={a['fn']} | "
            f"recall={a['recall']:.4f} fp_rate={a['fp_rate']:.4f} | "
            f"liq_pen={a['mean_liq_penalty']:.3f} | composite={a['composite']:+.4f}",
            flush=True,
        )
        results.append(output)

    return results


# ── Summary + hard-stop check ───────────────────────────────────────────────


def build_summary(results: list[dict], total_halts: int) -> str:
    lines = []
    lines.append("# Phase LULD-V3b T6 — Duration Sweep Results")
    lines.append("")
    lines.append(f"**Date:** 2026-06-19")
    lines.append(f"**Labeler:** T5 (5-min arithmetic mean + 1% sticky filter)")
    lines.append(f"**Proximity threshold:** {PROXIMITY_THRESHOLD:.3f}")
    lines.append(f"**Weights:** w_recall={W_RECALL} / w_fp={W_FP} / w_liq={W_LIQ}")
    lines.append(f"**Total halts detected by T5 labeler:** {total_halts}")
    lines.append("")

    # Hard-stop checks
    hs_triggered = []
    if total_halts <= 1:
        hs_triggered.append(f"halt_count={total_halts} <= 1")
    if total_halts < HS_MIN_HALTS:
        hs_triggered.append(f"total_halts={total_halts} < {HS_MIN_HALTS}")

    max_recall = max(r["aggregate"]["recall"] for r in results)
    if max_recall < HS_MIN_RECALL:
        hs_triggered.append(f"max_recall={max_recall:.4f} < {HS_MIN_RECALL}")

    min_liq = min(r["aggregate"]["mean_liq_penalty"] for r in results)
    if min_liq > HS_MAX_LIQ_PENALTY:
        hs_triggered.append(f"min_mean_liq_penalty={min_liq:.3f} > {HS_MAX_LIQ_PENALTY}")

    if hs_triggered:
        lines.append("## *** HARD STOP ***")
        lines.append("")
        lines.append("The following hard-stop criteria were triggered:")
        for hs in hs_triggered:
            lines.append(f"  - {hs}")
        lines.append("")
        lines.append("Do not proceed to T6 winner selection or Phase H.")
        lines.append("Root cause investigation required before continuing.")
        lines.append("")
    else:
        lines.append("## Hard-Stop Check: PASS")
        lines.append("")

    lines.append("## Duration Sweep Table")
    lines.append("")
    lines.append("| dur_sec | n_fires | n_halts | tp | fp | fn | recall | fp_rate | mean_liq_pen | composite |")
    lines.append("|--------:|--------:|--------:|---:|---:|---:|-------:|--------:|-------------:|----------:|")

    for r in results:
        c = r["config"]
        a = r["aggregate"]
        lines.append(
            f"| {c['luld_exit_duration_sec']:7.0f} "
            f"| {a['n_fires']:7d} "
            f"| {a['n_halts']:7d} "
            f"| {a['tp']:2d} "
            f"| {a['fp']:2d} "
            f"| {a['fn']:2d} "
            f"| {a['recall']:6.4f} "
            f"| {a['fp_rate']:7.4f} "
            f"| {a['mean_liq_penalty']:12.3f} "
            f"| {a['composite']:+9.4f} |"
        )

    lines.append("")
    lines.append("## V3 Baseline Comparison")
    lines.append("")
    lines.append("| Config | n_fires | n_halts | recall | composite |")
    lines.append("|--------|--------:|--------:|-------:|----------:|")
    lines.append("| V3 dur=0 (30s VWAP labeler) | 888 | 1 | 0.0000 | -34.1809 |")
    v3b_dur0 = next((r for r in results if r["config"]["luld_exit_duration_sec"] == 0), None)
    if v3b_dur0:
        a = v3b_dur0["aggregate"]
        lines.append(
            f"| V3b dur=0 (T5 labeler) | {a['n_fires']} | {a['n_halts']} "
            f"| {a['recall']:.4f} | {a['composite']:+.4f} |"
        )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("**Cooper selects the winner. Do not proceed to T6 charting or Phase H without explicit approval.**")

    return "\n".join(lines)


# ── Main ────────────────────────────────────────────────────────────────────


def main() -> None:
    V3B_RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # Load the same 100 events used in Phase LULD-V3
    v3_labels_path = V3_RESULTS_DIR / "halt_labels.json"
    if not v3_labels_path.exists():
        print(f"ERROR: V3 halt_labels.json not found at {v3_labels_path}")
        sys.exit(1)

    with open(v3_labels_path) as f:
        v3_label_data = json.load(f)

    events = v3_label_data["events"]
    print(f"Loaded {len(events)} events from Phase LULD-V3 sample (seed=42, split=val)")

    # Stage 1: Re-label (skip if already done)
    v3b_labels_path = V3B_RESULTS_DIR / "halt_labels.json"
    if v3b_labels_path.exists():
        print(f"\nStage 1 already complete — reusing {v3b_labels_path}")
        with open(v3b_labels_path) as f:
            label_data = json.load(f)
    else:
        label_data = run_stage1(events)
    total_halts = label_data["meta"]["total_halts"]

    # Stage 2: Duration sweep (load-once-replay-N)
    print(f"\n{'='*60}")
    print(f"STAGE 2 — Duration sweep {DURATION_SWEEP_SEC} (load-once-replay-N)")
    print(f"{'='*60}")

    all_results = run_duration_sweep(events, label_data, [float(d) for d in DURATION_SWEEP_SEC])

    # Summary
    summary_md = build_summary(all_results, total_halts)
    out_path = V3B_RESULTS_DIR / "t6_summary.md"
    out_path.write_text(summary_md, encoding="utf-8")

    print(f"\n{'='*60}")
    print("T6 COMPLETE")
    print(f"  halt_labels: {V3B_RESULTS_DIR / 'halt_labels.json'}")
    print(f"  summary:     {out_path}")
    print(f"{'='*60}")
    print(summary_md)


if __name__ == "__main__":
    main()
