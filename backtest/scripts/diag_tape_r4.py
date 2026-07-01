"""
Phase REBUILD-VAL T6 — DIAG-TAPE re-run on val_r4_stratified.json.

Gap-origin classification (T1_POSTMARKET / OVERNIGHT_NO_TAPE / T_PREMARKET / UNKNOWN)
for all 100 events in the r4 sample. No chart generation — classification and summary
only.

Writes: phase_diag_tape_r4/data_availability.json, summary.md
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal
import pyarrow.parquet as pq
import pytz

BACKTEST = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKTEST))
sys.path.insert(0, str(BACKTEST.parent))

from data.schemas.mom_db import FILTERED_DIR, NS_PER_SECOND
from data.loaders.prev_close import get_prev_close

OUT    = BACKTEST / "results" / "phase_diag_tape_r4"
SAMPLE = BACKTEST / "data" / "val_r4_stratified.json"

OUT.mkdir(parents=True, exist_ok=True)

ET    = pytz.timezone("America/New_York")
NYSE  = mcal.get_calendar("NYSE")


def prior_trading_day(date_str: str) -> Optional[str]:
    date = pd.Timestamp(date_str)
    start = date - pd.Timedelta(days=12)
    sched = NYSE.schedule(
        start_date=start.strftime("%Y-%m-%d"),
        end_date=(date - pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
    )
    if sched.empty:
        return None
    return sched.index[-1].strftime("%Y-%m-%d")


def find_event_path(ticker: str, date: str) -> Optional[Path]:
    candidates = list(FILTERED_DIR.glob(f"{ticker}_{date}_*"))
    return sorted(candidates)[-1] if candidates else None


def load_full_trades(event_path: Path) -> Optional[pd.DataFrame]:
    trades_path = event_path / "trades.parquet"
    if not trades_path.exists():
        return None
    table = pq.read_table(str(trades_path), columns=["sip_timestamp", "price", "size"])
    df = pd.DataFrame({
        "ts_ns": table.column("sip_timestamp").to_numpy().astype(np.int64),
        "price": table.column("price").to_numpy().astype(np.float64),
        "size":  table.column("size").to_numpy().astype(np.int64),
    })
    df.sort_values("ts_ns", inplace=True)
    df.reset_index(drop=True, inplace=True)
    df["dt_et"] = pd.to_datetime(df["ts_ns"], unit="ns", utc=True).dt.tz_convert(ET)
    df["date_et"] = df["dt_et"].dt.date.astype(str)
    return df


def classify_gap(
    df: pd.DataFrame,
    t1_date: str,
    event_date: str,
    price_at_t1_close: Optional[float],
    price_at_first_t_trade: Optional[float],
) -> str:
    d = df["date_et"]
    h = df["dt_et"].dt.hour
    m = df["dt_et"].dt.minute

    t1_pm  = (d == t1_date) & (h >= 16) & (h < 20)
    t_pre  = (d == event_date) & (h >= 4) & ((h < 9) | ((h == 9) & (m < 30)))

    n_t1_pm = int(t1_pm.sum())
    n_t_pre = int(t_pre.sum())

    if n_t1_pm > 0 and price_at_t1_close and price_at_t1_close > 0:
        t1_pm_end = float(df.loc[t1_pm, "price"].iloc[-1])
        if (t1_pm_end / price_at_t1_close - 1) > 0.10:
            return "T1_POSTMARKET"

    if n_t_pre >= 5 and price_at_t1_close and price_at_t1_close > 0:
        t_pre_prices = df.loc[t_pre, "price"]
        first_pre = float(t_pre_prices.iloc[0])
        last_pre  = float(t_pre_prices.iloc[-1])
        if first_pre < price_at_t1_close * 1.15 and last_pre >= price_at_t1_close * 1.30:
            return "T_PREMARKET"

    if (price_at_first_t_trade is not None and price_at_t1_close is not None
            and price_at_t1_close > 0):
        if (price_at_first_t_trade / price_at_t1_close - 1) > 0.10:
            return "OVERNIGHT_NO_TAPE"

    return "UNKNOWN"


def analyze_event(ev: dict) -> dict:
    tk, dt = ev["ticker"], ev["date"]
    stratum = ev.get("stratum")

    event_path = find_event_path(tk, dt)
    if event_path is None:
        return {"ticker": tk, "date": dt, "stratum": stratum, "gap_occurs_in": "UNKNOWN",
                "error": "no_event_dir"}

    df = load_full_trades(event_path)
    if df is None or len(df) == 0:
        return {"ticker": tk, "date": dt, "stratum": stratum, "gap_occurs_in": "UNKNOWN",
                "error": "no_trades"}

    t1_date = prior_trading_day(dt)
    if t1_date is None:
        return {"ticker": tk, "date": dt, "stratum": stratum, "gap_occurs_in": "UNKNOWN",
                "error": "no_t1_date"}

    d = df["date_et"]
    h = df["dt_et"].dt.hour
    m = df["dt_et"].dt.minute
    t1_rth = (d == t1_date) & ((h == 9) & (m >= 30) | (h >= 10) & (h < 16))
    t1_pm  = (d == t1_date) & (h >= 16) & (h < 20)
    t_pre  = (d == dt) & (h >= 4) & ((h < 9) | ((h == 9) & (m < 30)))
    t_sess = (d == dt) & (h >= 4) & (h < 20)

    t1_rth_df = df[t1_rth]
    price_at_t1_close = float(t1_rth_df["price"].iloc[-1]) if len(t1_rth_df) > 0 else None

    t_sess_df = df[t_sess]
    if len(t_sess_df) == 0:
        t_sess_df = df[d == dt]

    price_at_first = float(t_sess_df["price"].iloc[0]) if len(t_sess_df) > 0 else None
    first_trade_et = t_sess_df["dt_et"].iloc[0].isoformat() if len(t_sess_df) > 0 else None

    gap_from_t1 = None
    if price_at_t1_close and price_at_first and price_at_t1_close > 0:
        gap_from_t1 = round((price_at_first / price_at_t1_close - 1) * 100, 2)

    gap_occurs_in = classify_gap(df, t1_date, dt, price_at_t1_close, price_at_first)

    try:
        prev_close = get_prev_close(tk, dt)
    except Exception:
        prev_close = None

    return {
        "ticker": tk,
        "date":   dt,
        "stratum": stratum,
        "mom_pct": ev.get("mom_pct"),
        "gap_pct_at_hit": ev.get("gap_pct_at_hit"),
        "prev_close": round(float(prev_close), 4) if prev_close is not None else None,
        "sub_dollar": bool(prev_close is not None and prev_close < 1.0),
        "gap_occurs_in": gap_occurs_in,
        "gap_from_t1_close_pct": gap_from_t1,
        "n_trades_t1_postmarket": int(t1_pm.sum()),
        "n_trades_t_premarket":   int(t_pre.sum()),
        "first_trade_wall_clock_et": first_trade_et,
        "price_at_t1_close":  price_at_t1_close,
        "price_at_first_trade": price_at_first,
    }


def main():
    sample = json.load(open(SAMPLE))["events"]
    print(f"DIAG-TAPE r4: {len(sample)} events from {SAMPLE.name}")

    results = []
    for i, ev in enumerate(sample):
        tk, dt = ev["ticker"], ev["date"]
        print(f"  [{i+1:3d}/{len(sample)}] {tk} {dt}", end=" ... ", flush=True)
        try:
            res = analyze_event(ev)
            print(f"gap={res['gap_occurs_in']}  n_pre={res.get('n_trades_t_premarket')}")
        except Exception as e:
            import traceback
            traceback.print_exc()
            res = {"ticker": tk, "date": dt, "stratum": ev.get("stratum"),
                   "gap_occurs_in": "UNKNOWN", "error": str(e)}
            print(f"ERROR: {e}")
        results.append(res)

    (OUT / "data_availability.json").write_text(json.dumps(results, indent=2, default=str))

    # ── Summary ────────────────────────────────────────────────────────────
    ok      = [r for r in results if not r.get("error")]
    errors  = [r for r in results if r.get("error")]
    STRATA  = ("low", "mid", "high")
    CATS    = ["T1_POSTMARKET", "OVERNIGHT_NO_TAPE", "T_PREMARKET", "UNKNOWN"]

    # cross-tab: gap_occurs_in × stratum
    xtab = {s: {c: 0 for c in CATS} for s in STRATA}
    for r in ok:
        st = r.get("stratum", "unknown")
        cat = r.get("gap_occurs_in", "UNKNOWN")
        if st in xtab:
            xtab[st][cat] += 1

    # sub-$1 per stratum
    sub1 = {s: {"n_sub1": 0, "n_total": 0} for s in STRATA}
    for r in ok:
        st = r.get("stratum")
        if st in sub1:
            sub1[st]["n_total"] += 1
            if r.get("sub_dollar"):
                sub1[st]["n_sub1"] += 1

    overall = Counter(r.get("gap_occurs_in", "UNKNOWN") for r in ok)

    print("\nGap origin × stratum:")
    hdr = "        " + " ".join(f"{c[:16]:>18}" for c in CATS) + "  sub-$1"
    print(hdr)
    for s in STRATA:
        frac = f"{sub1[s]['n_sub1']}/{sub1[s]['n_total']}"
        row = f"  {s:<5} " + " ".join(f"{xtab[s][c]:>18}" for c in CATS) + f"  {frac}"
        print(row)
    print("\nOverall:")
    for cat in CATS:
        n = overall.get(cat, 0)
        print(f"  {cat:<22}: {n:3d}  ({100*n/max(len(ok),1):.1f}%)")

    # ── Write summary.md ────────────────────────────────────────────────
    xtab_md  = "| Stratum | " + " | ".join(CATS) + " | sub-$1 frac |\n"
    xtab_md += "|---------|" + "|".join(["---"] * (len(CATS) + 1)) + "|\n"
    for s in STRATA:
        frac = f"{sub1[s]['n_sub1']}/{sub1[s]['n_total']}"
        xtab_md += f"| {s} | " + " | ".join(str(xtab[s][c]) for c in CATS) + f" | {frac} |\n"

    overall_md = "| Category | N | % |\n|---|---|---|\n"
    for cat in CATS:
        n = overall.get(cat, 0)
        overall_md += f"| {cat} | {n} | {100*n/max(len(ok),1):.1f}% |\n"

    md = f"""---
tags:
  - type/results
  - domain/backtest
  - project/hawkes-ofi-impact
  - status/complete
created: 2026-06-30
phase: REBUILD-VAL T6 (DIAG-TAPE r4)
---

# Phase DIAG-TAPE r4 — Gap Origin Classification on val_r4_stratified

Sample: `val_r4_stratified.json` (n=100, mom_pct tercile strata 30/40/30)
Events analysed: {len(ok)} | Errors: {len(errors)}

## Gap Origin × Stratum

{xtab_md}

## Overall Distribution

{overall_md}

## Errors

{"None." if not errors else chr(10).join(f"- {e['ticker']} {e['date']}: {e.get('error')}" for e in errors)}
"""
    (OUT / "summary.md").write_text(md, encoding="utf-8")
    print(f"\nWrote {OUT / 'data_availability.json'} and {OUT / 'summary.md'}")


if __name__ == "__main__":
    main()
