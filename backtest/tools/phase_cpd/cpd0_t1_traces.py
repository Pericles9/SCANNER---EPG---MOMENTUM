"""
Phase CPD — Sub-phase CPD-0, Task T1
=====================================
Extract tick-level WJI and WJI_background traces for all 100 val events on the
**halt-adjusted active-seconds** axis, and pickle them for downstream CPD-0 tasks
(T2 log-ratio / symmetry, T3 PELT segmentation).

Locked design decisions (resolved with Cooper before T1 — see Phase CPD plan):
--------------------------------------------------------------------------------
1. WJI_background(t) ≡ 1.0  (Cooper Option A).
   The implemented WJI signal is ALREADY background-normalised against static
   references (λ_V_ref = pre-event mean λ_V, μ_buy = cold-start Hawkes value), so
   WJI rests at ≈ 1.0 by construction. There is no separable dynamic background to
   divide by; the constant-1.0 background means the downstream log-ratio reduces to
   WJI_log(t) = log(WJI(t)), which already rests at ≈ 0 (the CPD H0 mean). We still
   store a `wji_background` array of ones for interface uniformity with T2.

2. Timestamp axis = halt-adjusted ACTIVE seconds since T_event  (Cooper decision).
   Halts are detected with core.features.luld_halt_detection.prepare_active_trades
   (include_extended=True, to retain the 04:00–20:00 ET pre/post-market trades these
   events live in). The active-seconds axis compresses out halt gaps. For internal
   consistency the WJI EMAs (λ_V half-life τ_v, λ_buy_slow rate β_slow) are decayed on
   the active-seconds dt — i.e. no decay accumulates across a halt. This is the
   halt-adjustment the proposal mandates ("all EMA dt values use halt-adjusted active
   seconds"), NOT a change to the WJI formula. Consequence (accepted by Cooper): these
   traces are NOT bit-identical to the raw-seconds WJI-OPT baseline.

WJI formula (unchanged / locked):
    norm_v(t)   = λ_V(t)        / λ_V_ref        (λ_V_ref = pre-event mean λ_V, active axis)
    norm_buy(t) = λ_buy_slow(t) / μ_buy          (μ_buy   = cold-start Hawkes, from cache)
    WJI(t)      = max(norm_v, ε)^α · max(norm_buy, ε)^(1−α)        α = 0.50, ε = 1e-9

Inputs
------
- results/phase_wji_poc/quality_sample_val.json   — 100 val events (ticker, date, mom_pct)
- results/phase_wji_poc/.cache_val_results.json   — per-event t_event (raw sec from first
                                                    trade) and μ_buy (cold-start)
- config/q_bar_tiers.json                          — OFI q_bar fallback config

Output
------
- results/phase_cpd/cpd0/wji_traces.pkl
    dict keyed by "TICKER|DATE" → trace dict (see _trace_record_doc below).
- results/phase_cpd/cpd0/t1_summary.json
    human-readable per-event + aggregate summary (n_ticks, spans, halt stats, WJI stats).

Stored trace record fields (units)
----------------------------------
  ticker, date, mom_pct           — event identity
  t_event_raw_sec   (s)           — T_event in raw seconds-from-first-trade (cache value)
  t_event_active_sec(s)           — T_event mapped onto the active-seconds axis
  lv_ref            ($/s · ln2/τ) — pre-event mean λ_V on the active axis (WJI denominator)
  mu_buy            (events/s)    — cold-start μ_buy (WJI buy-side denominator)
  n_halts           (count)       — LULD halts detected in this session
  halt_seconds      (s)           — total halted wall-clock time removed from the axis
  n_ticks           (count)       — number of (active) trades in the trace
  arrays (all length n_ticks, aligned):
    t_active_sec        (s)  — active seconds since first active trade
    t_since_event_active(s)  — active seconds since T_event (negative pre-event)
    wji                 (-)  — WJI(t)  (rests ≈ 1.0 at background)
    wji_background      (-)  — ≡ 1.0  (Cooper Option A)

Run
---
    "D:/Trading Research/.venv/Scripts/python.exe" -m tools.phase_cpd.cpd0_t1_traces

No escalation criteria for T1 (per plan).
"""
from __future__ import annotations

import json
import logging
import math
import os
import pickle
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from data.loaders.trades import load_trades
from data.loaders.quotes import load_quotes
from core.ofi.trade_ofi import compute_trade_ofi
from core.features.luld_halt_detection import (
    prepare_active_trades,
    filter_index_to_intervals,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

LN2 = math.log(2)
EPS = 1e-9

# WJI signal params — fixed at the WJI-OPT / WJI-POC defaults (signal is locked).
TAU_V = 180.0
BETA_SLOW = 0.01
ALPHA = 0.50

OUT_DIR = REPO_ROOT / "results" / "phase_cpd" / "cpd0"
SAMPLE_PATH = REPO_ROOT / "results" / "phase_wji_poc" / "quality_sample_val.json"
CACHE_PATH = REPO_ROOT / "results" / "phase_wji_poc" / ".cache_val_results.json"
QBAR_PATH = REPO_ROOT / "config" / "q_bar_tiers.json"

MAX_WORKERS = 8
BATCH_SIZE = 25


# ══════════════════════════════════════════════════════════════════════
#  Active-seconds axis construction
# ══════════════════════════════════════════════════════════════════════

def _build_active_axis(td) -> tuple[np.ndarray, np.ndarray, int, float]:
    """
    Detect LULD halts and build the halt-adjusted active-seconds axis.

    The trades index is converted to **ET-naive** datetimes so the module's
    extended-hours sessions (built as naive 04:00–20:00) align with the data.
    include_extended=True keeps the full 04:00–20:00 ET window (pre/post-market),
    which these momentum events depend on.

    Returns
    -------
    mask : np.ndarray[bool], shape (N,)
        True for trades retained on the active axis (those NOT inside a halt window).
    active_seconds : np.ndarray[float], shape (mask.sum(),)
        Active seconds since the first retained trade (halt gaps compressed out).
    n_halts : int
        Number of detected halts.
    halt_seconds : float
        Total halted wall-clock seconds removed from the axis.
    """
    idx_et = (
        pd.to_datetime(td.timestamps, utc=True)
        .tz_convert("America/New_York")
        .tz_localize(None)
    )
    df = pd.DataFrame({"price": td.prices, "size": td.sizes}, index=idx_et)
    _, active_seconds, meta = prepare_active_trades(
        df, price_col="price", size_col="size", include_extended=True
    )
    # Recover the boolean mask over the FULL trade array so we can align sides/prices.
    mask = filter_index_to_intervals(df.index, meta["active_intervals"])
    n_halts = len(meta["halts"])
    halt_seconds = float(sum(h.duration_seconds() for h in meta["halts"]))
    return mask, np.asarray(active_seconds, dtype=np.float64), n_halts, halt_seconds


def _map_t_event_to_active(
    t_sec_active_axis: np.ndarray, active_seconds: np.ndarray, t_event_raw: float
) -> Optional[float]:
    """
    Map T_event (raw seconds-from-first-trade) onto the active-seconds axis.

    t_sec_active_axis[i] is the RAW seconds-from-first-trade of the i-th retained
    trade; active_seconds[i] is its active-seconds coordinate. We take the active
    coordinate of the last retained trade whose raw time is ≤ T_event. If T_event
    precedes the first retained trade, fall back to active_seconds[0].

    Returns None if there are no retained trades.
    """
    if len(active_seconds) == 0:
        return None
    pos = int(np.searchsorted(t_sec_active_axis, t_event_raw, side="right")) - 1
    if pos < 0:
        return float(active_seconds[0])
    if pos >= len(active_seconds):
        pos = len(active_seconds) - 1
    return float(active_seconds[pos])


# ══════════════════════════════════════════════════════════════════════
#  WJI on the active-seconds axis
# ══════════════════════════════════════════════════════════════════════

def _compute_wji_active(
    prices: np.ndarray,
    sizes: np.ndarray,
    sides: np.ndarray,
    active_seconds: np.ndarray,
    t_event_active: float,
    mu_buy: float,
    tau_v: float = TAU_V,
    beta_slow: float = BETA_SLOW,
    alpha: float = ALPHA,
) -> tuple[np.ndarray, float]:
    """
    Compute the WJI trace on the active-seconds axis (EMAs decay on active dt).

    Identical in structure to phase_wji_opt.common.compute_wji_signal, except dt is
    taken from `active_seconds` (halt gaps already compressed out) rather than raw
    wall-clock seconds. λ_V_ref is the pre-event mean of λ_V over active ticks with
    active time < t_event_active.

    Returns (wji_array, lv_ref). wji_array[i] aligns with active_seconds[i].
    """
    n = len(active_seconds)
    decay_v = LN2 / tau_v
    mu_buy_safe = max(float(mu_buy), EPS)

    # ── Pass 1: λ_V EMA over the active axis → pre-event mean reference ──
    lv = 0.0
    last_t: Optional[float] = None
    lv_series = np.empty(n, dtype=np.float64)
    for i in range(n):
        t = float(active_seconds[i])
        dv = float(prices[i]) * float(sizes[i])
        if last_t is None:
            lv = dv * decay_v
        else:
            dt = max(0.0, t - last_t)
            lv = lv * math.exp(-decay_v * dt) + dv * decay_v
        last_t = t
        lv_series[i] = lv

    pre_mask = active_seconds < t_event_active
    if pre_mask.any():
        lv_ref = max(float(lv_series[pre_mask].mean()), EPS)
    else:
        lv_ref = EPS

    # ── Pass 2: WJI = geometric mean of normalised components ──
    lb = 0.0
    last_t = None
    wji = np.empty(n, dtype=np.float64)
    for i in range(n):
        t = float(active_seconds[i])
        if last_t is None:
            lb = 0.0
        else:
            dt = max(0.0, t - last_t)
            lb *= math.exp(-beta_slow * dt)
        if int(sides[i]) == 1:
            lb += beta_slow
        last_t = t

        norm_v = lv_series[i] / lv_ref
        norm_b = lb / mu_buy_safe
        wji[i] = max(norm_v, EPS) ** alpha * max(norm_b, EPS) ** (1.0 - alpha)

    return wji, lv_ref


# ══════════════════════════════════════════════════════════════════════
#  Per-event worker
# ══════════════════════════════════════════════════════════════════════

def cpd0_t1_worker(args: dict) -> dict:
    """
    Extract the WJI/active-seconds trace for one val event.

    args keys: ticker, date, mom_pct, t_event (raw sec), mu_buy, q_bar_cfg.
    Returns a trace record dict (status='ok') or a skip/error record.
    """
    ticker = args["ticker"]
    date = args["date"]
    mom_pct = args["mom_pct"]
    t_event_raw = args["t_event"]
    mu_buy = args["mu_buy"]
    q_bar_cfg = args["q_bar_cfg"]
    base = {"ticker": ticker, "date": date}

    try:
        td = load_trades(ticker, date, mom_pct)
        if td.n_trades < 30:
            return {**base, "status": "skipped", "reason": "insufficient_trades"}

        qd = load_quotes(ticker, date, mom_pct)
        if qd is None or qd.n_quotes < 10:
            return {**base, "status": "skipped", "reason": "insufficient_quotes"}

        # OFI sides — replicate the baseline call exactly (tier_qbar fallback = 250.0,
        # since the "median" key is absent from q_bar_tiers.json).
        tier_qbar = q_bar_cfg.get("wide", {}).get("median", 250.0)
        ofi = compute_trade_ofi(
            trade_timestamps=td.timestamps, trade_prices=td.prices,
            trade_sizes=td.sizes.astype(np.float64),
            quote_timestamps=qd.timestamps,
            quote_bid_prices=qd.bid_prices, quote_ask_prices=qd.ask_prices,
            quote_bid_sizes=qd.bid_sizes.astype(np.float64),
            quote_ask_sizes=qd.ask_sizes.astype(np.float64),
            window_sec=10.0, q_bar_fallback=tier_qbar,
        )
        sides_full = ofi.sides

        # Active-seconds axis (halt-adjusted).
        mask, active_seconds, n_halts, halt_seconds = _build_active_axis(td)
        if mask.sum() < 30:
            return {**base, "status": "skipped", "reason": "insufficient_active_trades"}

        prices_a = td.prices[mask]
        sizes_a = td.sizes[mask]
        sides_a = sides_full[mask]
        t_sec_a = td.t_sec[mask]  # raw sec-from-first-trade for the retained trades

        t_event_active = _map_t_event_to_active(t_sec_a, active_seconds, t_event_raw)
        if t_event_active is None:
            return {**base, "status": "skipped", "reason": "no_t_event_on_active_axis"}

        wji, lv_ref = _compute_wji_active(
            prices_a, sizes_a, sides_a, active_seconds, t_event_active, mu_buy,
        )

        t_since_event = active_seconds - t_event_active

        return {
            **base,
            "status": "ok",
            "mom_pct": mom_pct,
            "t_event_raw_sec": float(t_event_raw),
            "t_event_active_sec": float(t_event_active),
            "lv_ref": float(lv_ref),
            "mu_buy": float(mu_buy),
            "n_halts": int(n_halts),
            "halt_seconds": float(halt_seconds),
            "n_ticks": int(len(active_seconds)),
            "t_active_sec": active_seconds.astype(np.float64),
            "t_since_event_active": t_since_event.astype(np.float64),
            "wji": wji.astype(np.float64),
            "wji_background": np.ones_like(wji, dtype=np.float64),
        }

    except Exception as e:
        import traceback
        return {**base, "status": "error", "error": str(e),
                "traceback": traceback.format_exc()}


# ══════════════════════════════════════════════════════════════════════
#  Driver
# ══════════════════════════════════════════════════════════════════════

def _build_cache_lookup() -> dict[tuple[str, str], dict]:
    """Map (ticker, date) → {t_event, mu_buy} from the WJI-POC val cache."""
    cache = json.load(open(CACHE_PATH))
    lut: dict[tuple[str, str], dict] = {}
    for rec in cache:
        if rec.get("status") != "ok":
            continue
        if rec.get("t_event") is None or rec.get("mu_buy") is None:
            continue
        lut[(rec["ticker"], rec["date"])] = {
            "t_event": rec["t_event"], "mu_buy": rec["mu_buy"],
        }
    return lut


def _summarise(traces: dict[str, dict]) -> dict:
    """Build a JSON-serialisable per-event + aggregate summary."""
    per_event = []
    halt_events = 0
    total_halt_sec = 0.0
    all_wji_rest = []  # pooled rest-state (warmup) WJI for a quick sanity stat
    for key, tr in traces.items():
        wji = tr["wji"]
        tse = tr["t_since_event_active"]
        rest = wji[(tse >= 0.0) & (tse < 300.0)]  # warmup window [T_event, +300s active)
        if len(rest):
            all_wji_rest.append(rest)
        if tr["n_halts"] > 0:
            halt_events += 1
            total_halt_sec += tr["halt_seconds"]
        per_event.append({
            "ticker": tr["ticker"], "date": tr["date"],
            "n_ticks": tr["n_ticks"], "n_halts": tr["n_halts"],
            "halt_seconds": round(tr["halt_seconds"], 1),
            "t_event_active_sec": round(tr["t_event_active_sec"], 1),
            "active_span_sec": round(float(tr["t_active_sec"][-1]), 1),
            "lv_ref": tr["lv_ref"], "mu_buy": tr["mu_buy"],
            "wji_median": float(np.median(wji)),
            "wji_p99": float(np.percentile(wji, 99)),
            "n_warmup_ticks": int(len(rest)),
        })
    pooled = np.concatenate(all_wji_rest) if all_wji_rest else np.array([])
    agg = {
        "n_events": len(traces),
        "n_events_with_halts": halt_events,
        "total_halt_seconds": round(total_halt_sec, 1),
        "pooled_warmup_ticks": int(len(pooled)),
        "pooled_warmup_wji_median": float(np.median(pooled)) if len(pooled) else None,
        "pooled_warmup_wji_mean": float(pooled.mean()) if len(pooled) else None,
        "note": "wji_background ≡ 1.0 (Cooper Option A); axis = halt-adjusted active sec.",
    }
    return {"aggregate": agg, "per_event": sorted(per_event, key=lambda r: r["ticker"])}


def _write_json_atomic(data, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
    ) as f:
        json.dump(data, f, indent=2)
        tmp = Path(f.name)
    os.replace(str(tmp), str(path))


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    events = json.load(open(SAMPLE_PATH))["events"]
    lut = _build_cache_lookup()
    q_bar_cfg = json.load(open(QBAR_PATH))

    work = []
    missing = []
    for e in events:
        key = (e["ticker"], e["date"])
        c = lut.get(key)
        if c is None:
            missing.append(key)
            continue
        work.append({
            "ticker": e["ticker"], "date": e["date"], "mom_pct": e["mom_pct"],
            "t_event": c["t_event"], "mu_buy": c["mu_buy"], "q_bar_cfg": q_bar_cfg,
        })

    log.info("T1: %d events queued (%d missing from cache)", len(work), len(missing))
    if missing:
        log.warning("Missing from cache: %s", missing)

    traces: dict[str, dict] = {}
    skipped, errored = [], []
    t0 = time.time()
    done = 0
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(cpd0_t1_worker, a): (a["ticker"], a["date"]) for a in work}
        for fut in as_completed(futs):
            r = fut.result()
            done += 1
            if r.get("status") == "ok":
                traces[f"{r['ticker']}|{r['date']}"] = r
            elif r.get("status") == "skipped":
                skipped.append((r["ticker"], r["date"], r.get("reason")))
            else:
                errored.append((r["ticker"], r["date"], r.get("error")))
                log.error("ERROR %s %s: %s", r["ticker"], r["date"], r.get("error"))
            if done % 10 == 0:
                log.info("  %d/%d done (%.0fs)", done, len(work), time.time() - t0)

    # Write pickle (intermediate calibration artifact — pickle is acceptable per plan).
    pkl_path = OUT_DIR / "wji_traces.pkl"
    with open(pkl_path, "wb") as f:
        pickle.dump(traces, f, protocol=pickle.HIGHEST_PROTOCOL)

    summary = _summarise(traces)
    summary["aggregate"]["n_skipped"] = len(skipped)
    summary["aggregate"]["n_errored"] = len(errored)
    summary["skipped"] = skipped
    summary["errored"] = [(t, d) for t, d, _ in errored]
    _write_json_atomic(summary, OUT_DIR / "t1_summary.json")

    log.info("─" * 60)
    log.info("T1 complete: %d traces, %d skipped, %d errored (%.0fs)",
             len(traces), len(skipped), len(errored), time.time() - t0)
    log.info("  events with halts: %d, total halt time: %.0fs",
             summary["aggregate"]["n_events_with_halts"],
             summary["aggregate"]["total_halt_seconds"])
    log.info("  pooled warmup WJI median=%.4f mean=%.4f (n=%d)",
             summary["aggregate"]["pooled_warmup_wji_median"] or float("nan"),
             summary["aggregate"]["pooled_warmup_wji_mean"] or float("nan"),
             summary["aggregate"]["pooled_warmup_ticks"])
    log.info("  → %s", pkl_path)
    log.info("  → %s", OUT_DIR / "t1_summary.json")


if __name__ == "__main__":
    main()
