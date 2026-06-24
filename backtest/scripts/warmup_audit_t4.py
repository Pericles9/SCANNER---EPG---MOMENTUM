"""
Warmup Clock Audit — T4 instrumentation.

Single-event analysis for XBP 2023-12-04.
T_event is taken directly from per_trade.json (no Hawkes re-run).
Script finds scanner hit time by scanning trade prices, then computes
relative timing of T_event, warmup expiry, scanner hit, and entry.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

BACKTEST = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKTEST))

import numpy as np

from data.loaders.trades import list_events, load_trades, _session_ns_bounds

# ── Known values from per_trade.json ──────────────────────────────────────────
TICKER = "XBP"
DATE   = "2023-12-04"
PREV_CLOSE           = 23.75
SCANNER_MOM          = 0.30           # 30% threshold
ENTRY_T_SEC          = 20000.132124858
ENTRY_LAG_SEC        = 306.26969027099767
ENTRY_TS_NS          = 1701700512953505959
TIME_OF_DAY_SEC_ENTRY = 20112.953505959   # seconds from 4am at entry tick

EPG_WARMUP = 300.0
NS_PER_SEC = 1_000_000_000

OUT_DIR = BACKTEST / "results" / "warmup_audit"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def fmt_tod(s: float | None) -> str:
    if s is None:
        return "None"
    h_total = 4 + s / 3600
    h = int(h_total)
    m = int((h_total - h) * 60)
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:04.1f} ET  ({s:.1f}s from 4am)"


def main():
    print(f"=== Warmup Clock Audit T4: {TICKER} {DATE} ===\n")

    # ── 1. Derive T_event timing ───────────────────────────────────────────────
    # t_sec origin is first trade in session; time_of_day_sec origin is 4am.
    # Offset = time_of_day_sec - t_sec  (constant for the whole session).
    tod_offset = TIME_OF_DAY_SEC_ENTRY - ENTRY_T_SEC   # seconds from 4am to first trade
    print(f"tod_offset (first trade tod from 4am): {tod_offset:.3f}s  ({fmt_tod(tod_offset)})")

    t_event_t_sec = ENTRY_T_SEC - ENTRY_LAG_SEC          # anchor fire in t_sec frame
    t_event_tod   = t_event_t_sec + tod_offset            # anchor fire in tod frame
    warmup_expiry_tod = t_event_tod + EPG_WARMUP
    entry_tod = ENTRY_T_SEC + tod_offset                  # = TIME_OF_DAY_SEC_ENTRY

    print(f"\nT_event:")
    print(f"  t_sec          = {t_event_t_sec:.3f}s from first trade")
    print(f"  time_of_day    = {fmt_tod(t_event_tod)}")
    print(f"Warmup expiry    = {fmt_tod(warmup_expiry_tod)}")
    print(f"Entry            = {fmt_tod(entry_tod)}")
    print(f"entry_lag_from_t_event = {ENTRY_LAG_SEC:.2f}s   "
          f"(= warmup {EPG_WARMUP:.0f}s + {ENTRY_LAG_SEC - EPG_WARMUP:.2f}s to first PASS)\n")

    # ── 2. Load raw trades to find scanner hit time ────────────────────────────
    # Get mom_pct for this event from the scanner DB
    all_events = list_events(min_mom=50.0, require_date=True)
    ev = next(
        (e for e in all_events if e["ticker"] == TICKER and e["date"] == DATE),
        None,
    )
    if ev is None:
        print("ERROR: event not found in scanner DB")
        sys.exit(1)

    mom_pct = ev["mom_pct"]
    print(f"Event found in scanner DB: mom_pct={mom_pct:.1f}%\n")

    td = load_trades(TICKER, DATE, mom_pct)
    N = td.n_trades
    print(f"Loaded {N} trades for {TICKER} {DATE}\n")

    # session start in ns (4am ET)
    start_ns, _ = _session_ns_bounds(DATE)
    tod_sec_all = (td.timestamps - start_ns).astype(np.float64) / NS_PER_SEC

    # ── 3. Find scanner hit (first price >= prev_close * 1.30) ────────────────
    scanner_threshold = PREV_CLOSE * (1.0 + SCANNER_MOM)
    scanner_hit_idx = None
    for i, price in enumerate(td.prices):
        if price >= scanner_threshold:
            scanner_hit_idx = i
            break

    if scanner_hit_idx is not None:
        scanner_hit_tod = tod_sec_all[scanner_hit_idx]
        scanner_hit_price = float(td.prices[scanner_hit_idx])
        print(f"Scanner hit (price >= {scanner_threshold:.4f}):")
        print(f"  idx={scanner_hit_idx}  price={scanner_hit_price:.4f}")
        print(f"  time = {fmt_tod(scanner_hit_tod)}")

        # Gate state at scanner hit based purely on timing
        if scanner_hit_tod < t_event_tod:
            gate_at_scanner = "INACTIVE (anchor not yet fired)"
        elif scanner_hit_tod < warmup_expiry_tod:
            gate_at_scanner = "WARMUP (anchor fired, still in 300s warmup)"
        else:
            gate_at_scanner = "POST-WARMUP (PASS or FAIL, needs trade flow)"
    else:
        scanner_hit_idx = None
        scanner_hit_tod = None
        gate_at_scanner = "N/A — price never reached scanner threshold"
        print(f"WARNING: price never reached {scanner_threshold:.4f}")

    # ── 4. Lag from scanner hit to entry ──────────────────────────────────────
    lag_scanner_to_entry = (
        (entry_tod - scanner_hit_tod) if scanner_hit_tod is not None else None
    )
    lag_tevent_to_scanner = (
        (scanner_hit_tod - t_event_tod) if scanner_hit_tod is not None else None
    )

    print(f"\nTiming summary:")
    if lag_tevent_to_scanner is not None:
        direction = 'T_event AFTER scanner' if lag_tevent_to_scanner > 0 else 'T_event BEFORE scanner'
        print(f"  scanner hit -> T_event : {lag_tevent_to_scanner:+.1f}s  ({direction})")
    if lag_scanner_to_entry is not None:
        print(f"  scanner hit -> entry   : {lag_scanner_to_entry:+.1f}s")

    # ── 5. T4 table ───────────────────────────────────────────────────────────
    table = {
        "ticker": TICKER,
        "date": DATE,
        "prev_close": PREV_CLOSE,
        "mom_pct_in_scanner_db": mom_pct,
        "N_trades": N,
        # T_event (anchor fire)
        "t_event_t_sec": round(t_event_t_sec, 3),
        "t_event_tod_sec": round(t_event_tod, 3),
        "t_event_hms": fmt_tod(t_event_tod),
        # Warmup expiry
        "warmup_expiry_tod_sec": round(warmup_expiry_tod, 3),
        "warmup_expiry_hms": fmt_tod(warmup_expiry_tod),
        # Scanner hit
        "scanner_price_threshold": scanner_threshold,
        "scanner_hit_idx": scanner_hit_idx,
        "scanner_hit_tod_sec": round(scanner_hit_tod, 3) if scanner_hit_tod is not None else None,
        "scanner_hit_hms": fmt_tod(scanner_hit_tod) if scanner_hit_tod is not None else None,
        "gate_state_at_scanner_hit": gate_at_scanner,
        # Entry
        "entry_tod_sec": round(entry_tod, 3),
        "entry_hms": fmt_tod(entry_tod),
        "entry_lag_from_t_event_sec": round(ENTRY_LAG_SEC, 3),
        "entry_lag_from_scanner_hit_sec": (
            round(lag_scanner_to_entry, 1) if lag_scanner_to_entry is not None else None
        ),
        # Key audit questions
        "warmup_reset_at_scanner_hit": False,
        "warmup_reset_reason": (
            "No scanner hit time exists in runner. gate.activate() is called at "
            "T_event (anchor fire from historical replay). Scanner hit is "
            "independent of EPG warm-up logic."
        ),
        "scanner_hit_before_t_event": (
            bool(scanner_hit_tod < t_event_tod) if scanner_hit_tod is not None else None
        ),
        "scanner_hit_before_entry": (
            bool(scanner_hit_tod < entry_tod) if scanner_hit_tod is not None else None
        ),
    }

    print("\n=== T4 INSTRUMENTATION TABLE ===\n")
    for k, v in table.items():
        print(f"  {k:<50} {v}")

    out_path = OUT_DIR / "t4_event_table.json"
    with open(out_path, "w") as f:
        json.dump(table, f, indent=2, default=str)
    print(f"\nWritten to {out_path}")

    # ── 6. Price trace around key moments ─────────────────────────────────────
    print("\n=== Price trace around scanner hit ===")
    if scanner_hit_idx is not None:
        w = range(max(0, scanner_hit_idx - 3), min(N, scanner_hit_idx + 6))
        for i in w:
            pct = (td.prices[i] - PREV_CLOSE) / PREV_CLOSE * 100
            marker = " ← SCANNER HIT (first price >= threshold)" if i == scanner_hit_idx else ""
            print(f"  i={i:5d}  {fmt_tod(tod_sec_all[i])[:24]}  "
                  f"price={td.prices[i]:8.4f}  intraday={pct:+6.2f}%{marker}")

    print("\n=== Price trace near entry (i around entry_idx=135) ===")
    w2 = range(max(0, 130), min(N, 145))
    for i in w2:
        pct = (td.prices[i] - PREV_CLOSE) / PREV_CLOSE * 100
        marker = " ← ENTRY (fill next tick)" if i == 135 else ""
        print(f"  i={i:5d}  {fmt_tod(tod_sec_all[i])[:24]}  "
              f"price={td.prices[i]:8.4f}  intraday={pct:+6.2f}%{marker}")

    print("\n=== Price at T_event neighborhood ===")
    t_event_i_approx = None
    for i in range(N):
        if abs(td.t_sec[i] - t_event_t_sec) < 5.0:
            t_event_i_approx = i
            break
    if t_event_i_approx is not None:
        w3 = range(max(0, t_event_i_approx - 3), min(N, t_event_i_approx + 6))
        for i in w3:
            pct = (td.prices[i] - PREV_CLOSE) / PREV_CLOSE * 100
            marker = " ← T_event neighborhood" if i == t_event_i_approx else ""
            print(f"  i={i:5d}  {fmt_tod(tod_sec_all[i])[:24]}  "
                  f"price={td.prices[i]:8.4f}  intraday={pct:+6.2f}%{marker}")


if __name__ == "__main__":
    main()
