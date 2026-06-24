# EPG-Rapid — Warmup Clock Audit Findings

**Date:** 2026-06-22  
**Auditor:** Claude (automated)  
**Awaiting Cooper Review**

---

## T1 — Rapid runner entry path (call chain)

**File:** `backtest/runner_rapid.py`

```
main()
  └─ build work items from list_events() (stratified val sample, seed=42)
       └─ ProcessPoolExecutor.map(_process_event_rapid, work_items)
            └─ _process_event_rapid(args: dict)
                 ├─ load_trades(ticker, date, mom_pct)       # all session trades, 4am–8pm
                 ├─ compute_trade_ofi(...)
                 ├─ _hawkes_replay_with_refit(...)            # produces lambda_hat[N]
                 ├─ Loop 1 (EPG state computation):
                 │    for i in range(N):
                 │        t_ev = anchor.update(lambda_hat[i], t_sec[i])
                 │        if t_ev and not t_event_fired:
                 │            gate.activate(t_ev)             # warmup starts here
                 │        epg_states[i] = gate.update(dv, t_sec[i])
                 └─ Loop 2 (entry/exit state machine):
                      first_pass mode: enter on first i where epg_states[i] == PASS
```

**No phase separation.** The runner processes ALL session trades (4am–8pm ET) in one
continuous loop. There is no "replay phase" vs "live phase" distinction.

---

## T2 — Gate initialization

### T2a — What timestamp does `gate.activate()` receive?

`gate.activate(t_ev)` where `t_ev` is returned by `anchor.update()`.

In `backtest/core/epg/anchor.py`:
```python
def update(self, lambda_hat: float, timestamp: float) -> float | None:
    if self._fired:
        return self._t_event        # returns stored t_event after first crossing
    if lambda_hat > self._k * self._lambda_ref:
        self._t_event = timestamp   # timestamp = t_sec[i] at crossing tick
        self._fired = True
        return self._t_event
    return None
```

So `t_ev` = `td.t_sec[i]` at the tick where `lambda_hat` first exceeds
`k_multiplier * lambda_ref`. This is seconds-from-first-trade at that tick in the
historical replay — NOT scanner hit time, NOT midnight, NOT 4am.

### T2b — Is the gate fresh or historical-state?

The gate is constructed **fresh** at the top of `_process_event_rapid()`:
```python
gate = ParticipationGate(
    half_life_seconds=EPG_TAU,
    peak_threshold_p=epg_p_open,
    warmup_seconds=EPG_WARMUP,
    ...
)
```
State: `INACTIVE`. No historical gate state is passed in.

`gate.activate(t_ev)` then resets all internal state (from `gate.py`):
```python
def activate(self, t_event: float) -> None:
    self._active   = True
    self._state    = GateState.WARMUP
    self._t_event  = t_event
    self._last_timestamp = t_event
    self._R        = 0.0
    self._E        = 1.0
```
Warmup clock = `t_event` exactly. No legacy carry-over.

### T2c — Gate state at first post-scanner tick

**N/A as a concept.** There is no "scanner phase" in the rapid runner. The single
continuous loop processes all N trades. The gate may be INACTIVE, WARMUP, PASS, or
FAIL at the tick where the stock first reaches 30% — it depends entirely on whether
the anchor has already fired by that tick.

For XBP 2023-12-04 (T4 event): anchor fired at 9:30:07 ET; warmup expired at 9:35:07 ET;
scanner threshold was first crossed at 10:11:59 ET. Gate was already POST-WARMUP
(PASS or FAIL) 37 minutes before the scanner threshold was reached.

---

## T3 — Search for gate.reset() / gate.activate(scanner_hit_time)

Searched `backtest/runner_rapid.py` for any call to `gate.reset()`, `gate.activate()`,
or `ParticipationGate()` after the initial construction block.

**Result: none found.** The gate is:
1. Constructed once at line 506
2. Activated once at line 524 when `anchor.update()` first fires
3. Updated every tick via `gate.update(dv, t_sec[i])` — no further resets or reactivations

There is no scanner hit time concept in the runner. No variable named `scanner_hit`,
`scanner_time`, or similar exists anywhere in `runner_rapid.py`.

---

## T4 — Single-event instrumentation: XBP 2023-12-04

Event selected: first entry in `rapid_r0/per_trade.json`
(entry_lag_sec = 306.27s, entry_t_sec = 20000.13s).

| Field | Value |
|-------|-------|
| prev_close | $23.75 |
| mom_pct (scanner DB) | 60.9% |
| N trades loaded | 5,866 |
| n_halt_windows | 3 |
| **T_event** | **09:30:06.7 ET** (anchor fires at RTH open surge) |
| T_event (t_sec) | 19,693.86s from first trade |
| Warmup expiry | 09:35:06.7 ET (T_event + 300s) |
| **Scanner hit** (price >= $30.875) | **10:11:59.7 ET** (idx=902, price=$31.00) |
| Gate state at scanner hit | POST-WARMUP (warmup had expired 37 min earlier) |
| **Entry** | **09:35:13.0 ET** (first PASS tick after warmup) |
| entry_lag from T_event | 306.27s (= 300s warmup + 6.27s to first PASS) |
| entry_lag from scanner hit | **−2,206.7s (entry was 37 min BEFORE scanner hit)** |

### Price context

```
09:30:00–09:30:06 ET:  price ~$21.95–22.53  (down 5–8% from prev_close)   [T_event fires]
09:35:06 ET:           warmup expires
09:35:13 ET:           entry at $22.70 signal tick / $23.00 fill  (down 4.4%)
10:11:59 ET:           price first reaches $31.00 (+30.5%)         [scanner hit]
```

The anchor fires at the RTH open due to a surge in Hawkes intensity from order flow
activity, NOT because the price was elevated. At entry (9:35am), the stock was DOWN
4.4% from prev_close. The stock reached 30%+ momentum 37 minutes after entry.

### Raw output file

`results/warmup_audit/t4_event_table.json`

---

## T5 — Verdict: Is the warmup clock being reset at scanner hit time?

**NO. The warmup clock is NOT being reset at scanner hit time.**

**Evidence:**
1. `runner_rapid.py` has no scanner hit time variable — the concept does not exist in the runner.
2. `gate.activate()` is called exactly once, at T_event (the Hawkes intensity crossing tick).
3. `gate.reset()` is never called in the rapid runner.
4. T4 confirms: gate.activate() was called at 9:30:07 ET; scanner hit occurred at 10:11:59 ET — 41 minutes later. The warmup had already expired by the time the stock crossed 30%.

**The warmup clock is working exactly as designed.** It counts 300s from T_event
(Hawkes anchor fire), then allows entry on the first PASS tick.

**No warmup clock bug.** T6 (fix + re-run) is NOT triggered.

---

## Collateral Finding — Entry Before Scanner Hit

This audit uncovered a **design observation** that is separate from the warmup clock
question but materially relevant to R1 scoping:

> In XBP 2023-12-04, the backtest entered at 9:35am ET when the stock was DOWN 4.4%.
> The stock did not reach the 30% scanner threshold until 10:11am ET.
> **The backtest entered 37 minutes before the scanner would have fired.**

This is consistent with the R1 D diagnostic finding that **65.4% of events have anchor
firing before scanner hit** (median offset −173.7s = anchor fires 173s before scanner).
XBP 2023-12-04 is an extreme case (41 min gap), but the pattern is the same.

**This is not a bug.** The runner processes all-session trades so that the Hawkes model
sees full warm-up history. The anchor fire reflects the Hawkes intensity surge, which
can be driven by volume/flow rather than price level.

**Implication for R1 scope:** If we want entries to be conditioned on the stock being
at or above the 30% scanner threshold at time of entry (the "real-world" constraint),
a scanner-hit gate is needed. This would be a new feature, not a bug fix.

**Cooper's decision required on:** whether to gate entries to post-scanner-hit ticks only.

---

## Escalation Check

| Criterion | Status |
|-----------|--------|
| Rapid runner file cannot be located | PASS — found at `backtest/runner_rapid.py` |
| Gate state at first live tick is WARMUP after fix | N/A — no bug, no fix |
| n_trades < 50% of R1 trade count | N/A |
| mean_entry_lag > 300s after fix | N/A |

---

## Required Actions Before Proceeding

1. **Cooper reviews this document** before any R1 re-run or further phase work.
2. **Cooper decides** on the collateral finding: should entries be gated to
   post-scanner-hit ticks? If yes, this is a new C-phase scope item.
3. If Cooper approves no changes: R0 rebuild results stand; proceed to R1 per prior plan.

---

*Full instrumentation output: `results/warmup_audit/t4_event_table.json`*
