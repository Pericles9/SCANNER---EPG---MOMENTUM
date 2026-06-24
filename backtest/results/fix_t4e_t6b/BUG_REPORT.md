# Bug Report — FIX-T4E-T6B
Date: 2026-06-24
Source: AUDIT-RAPID findings T4e and T6b (`backtest/results/audit_rapid/findings.md`)

Two ❌ hard findings from the AUDIT-RAPID pass are corrected in a single commit to
`backtest/runner_rapid.py`. This document is written **before** the first code change and records
what was wrong, why it mattered, and the one-line fix for each. The exact file/line locations and
supporting evidence are reproduced from the audit.

---

## T4e — `max_entry_lag_sec` exits open positions prematurely

### What was wrong

The `max_entry_lag_sec` filter is implemented as an **unconditional `break` of the entire tick
loop** (`runner_rapid.py:590-594`), placed *before* the entry/exit dispatch (entry at `:604`,
exit at `:649`):

```python
max_lag = args.get("max_entry_lag_sec")
if (max_lag is not None and scanner_hit_t_sec is not None
        and td.t_sec[i] - scanner_hit_t_sec > max_lag):
    break
```

The intent of the parameter is **entry/scoring-side only**: "events where
`t_entry − t_scanner_hit > max_entry_lag_sec` are excluded from scoring … an entry/scoring
invalidation deadline only" (`EPG_Rapid_Strategy.md §2.1`). Abandoning *entry* once the deadline
passes is correct. But because the statement is a bare `break` with **no `not in_position`
guard**, it also terminates **exit monitoring of a position that is already open**.

Concretely: when a trade was entered early (e.g. at scanner+0s, gate already PASS at the scanner
hit) but its first post-entry PASS→FAIL transition does not occur until *after*
`scanner_hit + max_lag`, the loop `break`s at the deadline tick while still `in_position`. The
`epg_window_close` exit at `:649-651` is never reached. The open position falls through to the
post-loop fallback:

```python
# ── If still in position at session end ──   (runner_rapid.py:691-722)
if in_position:
    exit_price = float(td.prices[N - 1])
    exit_t_sec = td.t_sec[N - 1]
    ...
    "exit_reason": "session_end",
```

so it is force-booked as **`session_end` at the final tick of the data** (`td.prices[N-1]`,
`td.t_sec[N-1]`), regardless of where the EPG gate actually closed. The PASS→FAIL exit signal —
the thing `p_close` is supposed to control — is structurally bypassed for these trades.

This affects **only** runs with `max_entry_lag_sec is not None`. R0 (`max_entry_lag_sec=null`) is
unaffected because the `break` is guarded by `max_lag is not None`.

### Evidence from DIAG-GATE-2

The DIAG-GATE-2 baseline ran at `MAX_LAG_SEC=300` / `P_CLOSE=0.70` and observed "4/5 exits are
`session_end` despite PASS→FAIL transitions." From the 5 diagnostic events
(`results/phase_diag_gate/selected_events.json`, replay in `.../replay_data/`):

- **LRHC** (slot A): entered at scanner+11.4s; its gate went PASS→FAIL **0.13s later**
  (`exit_t_sec=36068.090`, idx 69377 FAIL ratio 0.69991, prior tick 69376 PASS ratio 0.70071) —
  inside the 300s window → exit booked as `epg_window_close`. ✅ caught.
- **MLGO** (B, lag 0), **MBIO** (C, lag 0), **VSSYW** (D, lag 0), **BDRX** (E, lag ≈261s): all
  entered within 300s, but their first post-entry PASS→FAIL fell **after** `scanner+300s`. The
  loop broke while still `in_position` → `session_end` booked at the final tick. Booked PnL
  +203%, +150.9%, −88.2%, +26.4% respectively — i.e. held to absolute end-of-data with the gate
  exit disabled.

LRHC succeeded only because its near-threshold ratio dip happened to fall 0.13s after entry, far
inside the deadline window. The gate chatters throughout every event (16 PASS→notPASS transitions
across the LRHC replay); the four `session_end` events simply had no PASS→FAIL inside their
deadline window, so the `break` cut exit monitoring off before any chatter could trigger an
`epg_window_close`.

### Impact on R1 results

The genuine T4e-affected R1 run is the MDR≥150 symmetric sweep at
`backtest/results/phase_r1_mdr150/sym_p{50..75}/` (`entry_mode=first_pass`,
`max_entry_lag_sec=300`, `p_open=p_close`, seed=42, val sample of 100). Its exit breakdown is
`session_end`-dominated and barely moves with `p`:

| p | session_end % | epg_window_close % | CVaR5 % | mean_hold_s |
|------|--------------|--------------------|---------|-------------|
| 0.50 | 100.0 | 0.0 | −68.73 | 39126 |
| 0.70 | 93.48 | 6.52 | −68.73 | 37491 |

Because the dominant exit for kept trades is `session_end` at end-of-data (independent of
`p_close`), the R1 sweep — whose stated purpose is to recalibrate `p_close`, which "directly
controls exit timing" (`EPG_Rapid_Strategy.md §2.1`) — was measuring **hold-to-end-of-data PnL**
for almost every trade, not gate-driven exits. The sweep was largely decoupled from the parameter
it was tuning, and `p_close` has never actually been exercised as an exit signal under
`max_entry_lag_sec`. R0 (`max_entry_lag_sec=null`) is unaffected.

> **Note on directory naming:** the phase prompt refers to the old R1 results as
> `backtest/results/phase_r1/`. That directory holds an **even older, separately-invalidated**
> run (`entry_mode=cross_and_hold`, no `max_entry_lag_sec`, pre-scanner-floor-fix — see
> `results/phase_r1/INVALIDATED.md`) which shows 100% `epg_window_close` and is *not* the
> T4e-affected run. The config-matched T4e baseline used for the R1-vs-R1-fixed comparison is
> `phase_r1_mdr150/sym_p*`. **Both** old directories are preserved untouched.

### Fix

Add `and not in_position` to the `break` condition so the deadline abandons **entry** but never
terminates monitoring of an already-open position. (`runner_rapid.py:590-594`.)

---

## T6b — Gate `λ_V` not halt-aware

### What was wrong

Per spec, the halt-gap clock pause must be applied to **both** the Hawkes EMA **and** the gate's
`λ_V`: "for any trade pair straddling a halt gap … substitute `dt = 0` (or epsilon) … so the
intensity **and `λ_V`** do not collapse across the suspension" (`EPG_Rapid_Strategy.md §6`;
`EPG_Rapid_Test_Phases.md C3 T3c`).

In the code the `dt_eff = 1e-6` substitution is applied **only inside
`_hawkes_replay_with_refit`**, to the Hawkes `R_buy`/`R_sell` decay (`runner_rapid.py:319-330`).
The gate's `λ_V` is updated separately in the precompute loop:

```python
dv = float(td.prices[i]) * float(td.sizes[i])
epg_states[i] = gate.update(dv, td.t_sec[i])     # runner_rapid.py:550-551 — raw timestamp
```

`ParticipationGate` in `peak` mode (the configured mode) computes its **own**
`dt = timestamp - self._last_timestamp` and applies full exponential decay across the gap
(`gate.py:398-404`), with no halt awareness — its `is_halted` parameter is consulted only in
`cusum`/`bocpd` modes and is never passed here. So the gate's `λ_V` **does** collapse across a
trading halt, contrary to spec §6.

### Impact

Bounded to events that contain a confirmed halt window (`detect_luld_halts` requires a gap
≥300s). On those events the participation ratio `λ_V / peak` decays artificially across the
suspension, which can spuriously close the EPG PASS window (a false `epg_window_close`) on the
first trade after the halt. It did not materially change the *interpretation* of the R1 results
(which were dominated by the unrelated T4e `session_end` issue), but it violates the spec and must
be corrected before any further sweep is trusted as exit calibration.

### Fix

In the gate precompute loop, compute a halt-adjusted `gate_t_sec` (= `t_prev + 1e-6` for any
inter-trade gap > `HALT_GAP_THRESHOLD` that overlaps a detected halt interval) and pass it to
`gate.update()` in place of the raw `td.t_sec[i]`, reusing the `halt_intervals` and
`HALT_GAP_THRESHOLD` already in scope. The `anchor.update()` call keeps the raw timestamp.
(`runner_rapid.py:550-551`.)

---

## Fix sequencing note

The two findings are independent. T4e is gated on `max_entry_lag_sec is not None` (affects R1+,
incl. the DIAG-GATE-2 baseline at `MAX_LAG=300`; not R0). T6b affects only events with a detected
halt window. Both are corrected in a single commit; the R1 T1 symmetric sweep is then re-run
against the fixed runner to `backtest/results/phase_r1_fixed/` so that `p_close` is exercised as
an exit signal for the first time. Old results in `phase_r1/` and `phase_r1_mdr150/` are
preserved untouched.
