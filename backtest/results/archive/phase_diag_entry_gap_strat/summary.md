# Phase DIAG-ENTRY — Entry Failure Summary

**Date:** 2026-06-29
**Scope:** R3 disabled arm, val_r3_stratified — 86 events, 80 processed (3 errored, 3 skipped),
28 traded. Read-only audit; full-session Hawkes/gate replay reproduced exactly (consistency
check: **0/80 disagree** with R3 trade/no-trade).

> **Audit note.** The replay is run over the **full session** (not truncated at scanner+300) so
> the cold-start Hawkes MLE — fit on the first 1000 ticks — matches the runner exactly; truncating
> would change `lambda_ref` and could disagree with R3. A built-in consistency check
> (`audit_would_trade` = `any PASS in [scanner, scanner+300]`, compared to R3's actual trade)
> passed for all 80 events.

## Failure reason distribution (of 80 processed)

| Failure reason | N | % of 80 |
|----------------|---|---------|
| ANCHOR_LATE | 35 | 43.8% |
| TRADED | 28 | 35.0% |
| WARMUP_AT_DEADLINE | 13 | 16.2% |
| NEVER_PASS_IN_WINDOW | 3 | 3.8% |
| PASS_TOO_LATE | 1 | 1.2% |
| ANCHOR_NEVER_FIRED | 0 | 0.0% |

(The 3 anchor-never-fired events are the 3 `no_t_event` **skipped** events, handled in the
excluded set — see below — not among the 80.)

## Stratum × failure reason cross-tab

| | TRADED | ANCHOR_NEVER_FIRED | ANCHOR_LATE | WARMUP_AT_DEADLINE | NEVER_PASS_IN_WINDOW | PASS_TOO_LATE |
|--|--|--|--|--|--|--|
| low | 28 | 0 | 8 | 7 | 3 | 1 |
| mid | 0 | 0 | 8 | 6 | 0 | 0 |
| high | 0 | 0 | 19 | 0 | 0 | 0 |
| **first-appear** | 0 | 0 | 30 | 7 | 0 | 0 |

Every trade comes from the low-gap stratum. All 19 high-gap and all 14 mid-gap processed events
fail at the anchor/warmup stage. All 37 first-appearance events fail (30 ANCHOR_LATE, 7
WARMUP_AT_DEADLINE); none trade.

## Pre-scanner history comparison

| Category | N | Mean n_trades_before_scanner | Mean lambda_ref | Mean t_event_relative (s) |
|----------|---|------------------------------|-----------------|---------------------------|
| TRADED | 28 | 38,000.2 | 2.3989 | **−7,849.0** (fires before scanner) |
| ANCHOR_LATE | 35 | 103.5 | 2.5729 | **+9,697.0** (fires after scanner) |
| WARMUP_AT_DEADLINE | 13 | 210.2 | 3.8309 | **+79.6** (fires just after scanner) |
| NEVER_PASS_IN_WINDOW | 3 | 30,602.3 | 0.4089 | −10,036.5 |
| PASS_TOO_LATE | 1 | 212.0 | 1.1135 | −0.0 |
| first-appearance (all) | 37 | 0.0 | 2.4171 | +8,366.0 |

Median `n_trades_before_scanner`: **TRADED = 7,502 vs non-TRADED = 0**. `lambda_ref` does **not**
separate the groups (TRADED 2.40 vs ANCHOR_LATE 2.57 — similar); the separating variable is
**pre-scanner trade history**, which drives *when the anchor fires relative to the scanner hit*.

## Root cause assessment

The binding constraint is **anchor/warmup timing, gated by pre-scanner trade history** — not
`lambda_ref` magnitude. Entry requires the EPG gate to be PASS within the 300s entry window
`[scanner, scanner+300]`. The gate can only be PASS after a 300s warmup that begins when the
EventAnchor fires, and the anchor fires only when Hawkes `λ_hat` first exceeds `k·λ_ref` (k=5),
which requires accumulated trade intensity. Events with rich pre-scanner history (TRADED: mean
~38,000 prior trades) build `λ_hat` up well before the scanner hit, so the anchor fires ~7,849s
*before* scanner, warmup completes, and the gate is already PASS at the scanner tick (entry lag
≈0). Events with sparse or no pre-scanner history (non-traded median = 0 prior trades) have no
`λ_hat` buildup until trading begins at/after the scanner hit, so the anchor fires *late*
(ANCHOR_LATE mean +9,697s; WARMUP_AT_DEADLINE mean +80s) and the 300s warmup extends past the 300s
entry deadline → no PASS in the window → no trade. `n_trades_before_scanner` cleanly separates
traded from non-traded events (median 7,502 vs 0). The Hawkes/warmup hypothesis is **confirmed by
the data.** — Data only. No fix recommendations. Cooper decides next step.

## First-appearance events

0 of 37 processed first-appearance events traded. All 37 have `n_trades_before_scanner = 0` and
`scanner_hit_t_sec = 0.00` — the scanner hit lands on the **first available trade tick** (the stock
opened already ≥30% above prior close, a pre-market gap-up), so no intraday trade history exists
before the hit. With no prior trades, `λ_hat` is at its cold-start floor at the scanner tick and
only builds once trading develops; the anchor therefore fires a mean **+8,366s after** the scanner
hit (30 ANCHOR_LATE) or just after it (7 WARMUP_AT_DEADLINE). The 300s warmup then always finishes
after the 300s entry deadline. First-appearance events cannot trade under the current
anchor→warmup→deadline timing, by construction.

## Scanner hit timing note

For every event with `n_trades_before_scanner = 0` (all 37 first-appearance events),
`scanner_hit_t_sec = 0.00` — i.e. the scanner hit is assigned to the **first tick of the event's
trade data**. These names gapped up overnight and their first print of the session is already
≥30% above the prior close, so the scanner hit is recorded before any intraday trade history can
accumulate. This is not a scanner-timing bug; it is the data reality for pre-market gap-ups, and
it is the structural reason the anchor cannot warm up before the entry deadline for this cohort.

## Excluded events (6)

| Ticker | Date | Status | Reason |
|--------|------|--------|--------|
| SKYE | 2023-12-06 | ERROR | missing quotes.parquet |
| SKYE | 2024-02-27 | ERROR | missing quotes.parquet |
| PSIX | 2024-07-02 | ERROR | missing quotes.parquet |
| ODVWZ | 2024-01-29 | SKIPPED | no_t_event (anchor never fired full session) |
| FBYDW | 2024-06-28 | SKIPPED | no_t_event |
| ALCYW | 2023-12-06 | SKIPPED | no_t_event |

## Deviation note

Per-event charts produced for a selected diagnostic subset only (12 events across the failure
categories + 2 traded controls), not for all 80 events. No new trade records — the standard
per-event chart requirement does not apply.

## Escalation status

No hard stops. The R3-consistency check passed (0/80 disagree) after a self-found audit bug
(missing `gate.activate()` on anchor fire) was fixed and the audit re-run. T1a: 0 first-appearance
events have `n_trades_before_scanner > 100` (all have exactly 0) — no flag. T5b: no
ANCHOR_NEVER_FIRED events among the 80, so the λ_hat-vs-threshold discrepancy check is vacuous.

## Results location

`backtest/results/phase_diag_entry/`
