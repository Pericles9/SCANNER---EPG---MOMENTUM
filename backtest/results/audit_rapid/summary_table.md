# Phase AUDIT-RAPID — Summary Table

**Date:** 2026-06-24 · Read-only audit · No backtest run · No per-event charts (analysis-only,
exempt per `Agent_Prompt_Standard.md §7`). Full detail: `findings.md`.

Counts below: a question is a "mismatch" if its Match is ❌ NO **or** ⚠️ PARTIAL.

## 1. Per-area summary

| Area | Files Read | Questions | Mismatches | Mismatch Labels |
|------|-----------|-----------|------------|-----------------|
| T1 — Scanner Hit Simulation | val_mdr150_diagnostic.json; build_scanner_hit_catalog.py; build_mdr200_sample.py; runner_rapid.py | 5 | 1 | T1e ⚠️ (exact-tick, no poll-cadence lag) |
| T2 — Floor Guard Placement | runner_rapid.py | 3 | 0 | — |
| T3 — Entry State Machine | runner_rapid.py; core/filters/rapid_entry.py | 5 | 1 | T3d ⚠️ (max_lag as loop `break`; entry-side OK, exit side-effect → T4e) |
| T4 — Exit State Machine | runner_rapid.py; phase_diag_gate/replay_data/LRHC_2024-06-14.json; phase_diag_gate/selected_events.json | 6 | 2 | T4e ❌ (max_lag `break` short-circuits exit stack → premature session_end); T4d ⚠️ (epg_window_close.enabled flag ignored) |
| T5 — Gate Wiring | runner_rapid.py; core/epg/gate.py | 4 | 0 | — |
| T6 — Halt Handling | runner_rapid.py; runner.py; core/epg/gate.py; core/features/luld_halt_detection.py | 3 | 2 | T6b ❌ (gate λ_V gets no halt dt-substitution); T6c ⚠️ (60s sub vs 300s detection) |
| T7 — Session Boundary | runner_rapid.py; config/strategy.json | 4 | 0 | — |
| T8 — ROC Gate | runner_rapid.py; config/strategy.json | 3 | 0 | — |
| **Total** | — | **33** | **6** | 2 ❌ hard, 4 ⚠️ partial |

## 2. Mismatch list

| ID | Divergence (one line) | File:line |
|----|------------------------|-----------|
| **T4e** ❌ | `max_entry_lag_sec` loop `break` terminates exit monitoring of an open position → force-books `session_end` at end-of-data; explains DIAG-GATE-2 "4/5 session_end". Active only when `max_lag` set (R1=300s). | `runner_rapid.py:590-594` (break) → `:691-722` (session_end fallback) |
| **T6b** ❌ | Halt-gap `dt=0` substitution applies to the Hawkes EMA only; the gate `λ_V` decays normally across halts (peak mode ignores `is_halted`), contrary to spec §6. | `runner_rapid.py:551` (gate update, raw ts) vs `:319-330` (Hawkes-only sub); `gate.py:398-404` |
| **T3d** ⚠️ | `max_entry_lag_sec` implemented as a loop `break`: entry-abandon semantics are correct (doesn't wait), but it is the same statement that causes the T4e exit side-effect. | `runner_rapid.py:590-594` |
| **T4d** ⚠️ | `epg_window_close_exit.enabled` config flag never read by the runner; EPG-close exit is hard-coded ON (behavior correct, flag inert). | `config/strategy.json:50-53`; exit unconditional at `runner_rapid.py:649-651` |
| **T6c** ⚠️ | dt-substitution gap floor = 60s (matches C3 spec) but `detect_luld_halts()` runs at default `halt_gap_seconds=300`; two unreconciled thresholds (60s floor is non-binding). | `runner_rapid.py:98,199,376`; `core/features/luld_halt_detection.py:91` |
| **T1e** ⚠️ | Scanner hit uses the exact crossing-tick timestamp, not a 15–30s poll-grid timestamp; optimistic entry timing vs. live scanner (documented, not a spec violation). | `build_scanner_hit_catalog.py:105` |

## 3. Output file status

| File | Description | Status |
|------|-------------|--------|
| `backtest/results/audit_rapid/findings.md` | Full findings, all 8 areas, per-question Spec/Code/Match/Evidence | ✅ written |
| `backtest/results/audit_rapid/summary_table.md` | This file | ✅ written |

## 4. Escalation status

No escalation triggered. `runner_rapid.py` exists; the val sample exists (with a
`scanner_hit_ts_ns`/`scanner_hit_tod_sec` equivalent of `t_scanner_hit_sec`); all Context files
were located (some at `(1)`-suffixed or `backtest/`-relative paths — see findings §"Path
reconciliation"). Per instructions, mismatches were reported without stopping.

## 5. Note for fix sequencing (data only — no recommendation)

The two ❌ findings are independent. T4e is gated on `max_entry_lag_sec is not None`, so it
affects R1+ runs (incl. the DIAG-GATE-2 baseline at `MAX_LAG=300`) but **not** R0
(`max_entry_lag_sec=null`). T6b affects only events with a detected halt window (gap ≥300s).
Awaiting Cooper review before any fix phase or follow-on run.
