# INVALIDATED — Phase R0 Results

**Invalidated:** 2026-06-22  
**Reason:** Scanner hit floor fix not yet applied when these runs were produced.

## What was wrong

The EPG-Rapid runner had no concept of a scanner hit timestamp. In 65.4% of val-split
events the Hawkes anchor fires at the RTH open intensity surge — before the stock has
reached the 30% momentum threshold that would trigger the live scanner. As a result,
the backtest was entering positions in stocks that were **not yet scanner hits** at entry
time, which is impossible in live trading.

Concrete example (XBP 2023-12-04): entry at 9:35am ET when stock was DOWN 4.4%.
Scanner threshold not reached until 10:11:59am ET — 37 minutes later.

## Affected runs

| Directory | Entry mode | Status |
|-----------|-----------|--------|
| `baseline_r0/` | rising_edge | **INVALID** |
| `rapid_r0/` | first_pass | **INVALID** |
| `_old/` | various | **INVALID** (pre-rebuild; doubly invalidated) |

## What replaces this

`results/scanner_floor_fix/` — post-fix baseline with `scanner_hit_ts_ns` floor applied.
Entry is blocked until the first tick at or after the scanner hit timestamp.

## Audit trail

- `results/warmup_audit/audit_findings.md` — T4 single-event timing table, T5 verdict
- `results/warmup_audit/t4_event_table.json` — raw XBP 2023-12-04 timing data
- `results/scanner_floor_fix/fix_summary.md` — what changed and where (written after fix)
