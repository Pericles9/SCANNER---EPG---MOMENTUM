---
tags:
  - type/results
  - domain/backtest
  - project/hawkes-ofi-impact
  - status/complete
created: 2026-07-01
phase: R1.5-Final
reference_config: "val_r4_stratified · p=0.80 · time-gate exit resweep"
---

# Phase R1.5-Final — T_gate (time-gate exit) Resweep on val_r4_stratified

**Reference:** `val_r4_stratified.json` (n=100, mom_pct tercile strata, 30/40/30, seed=42).
Fixed: p_open=p_close=0.80, `max_entry_lag_sec=500`, entry_mode=first_pass. Swept
`--t-gate-sec ∈ {300,400,500,600,750}`. LULD retired (not in the rapid exit stack).
Exit stack: `time_gate → epg_window_close → session_end`. 406 backtest tests pass.

**What "T_gate" means here:** the R1.5 single-shot **time-gate exit** (`runner_rapid.py:664-709`):
at the first tick ≥ T_gate seconds since entry, if open P&L < 0, cut the position (reason
`time_gate`); checked once, then disabled. This is the exit-side mechanism selected at 500s on
the retired MDR sample — **not** `max_entry_lag_sec` (the entry-lag cap, held at 500 throughout).
R1-Final never enabled `--t-gate-sec`, which is why every R1-Final exit was `epg_window_close`.

---

## T1 — Sweep table

| T_gate | n | PF | WR% | mean PnL% | CVaR5% | RTH n/PF | PRE n/PF | p90 lag(s) | window_close% | time_gate% | session_end% |
|--------|---|----|----|-----------|--------|----------|----------|------------|---------------|------------|--------------|
| **300** | 65 | **1.9141** | 44.62 | 2.369 | **−14.53** | 33/**2.886** | 32/**1.394** | 332 | 63.1 | 36.9 | 0.0 |
| 400 | 65 | 1.6415 | 47.69 | 1.992 | −23.31 | 33/2.472 | 32/1.180 | 332 | 66.2 | 33.9 | 0.0 |
| 500 | 65 | 1.5572 | 47.69 | 1.823 | −21.18 | 33/2.074 | 32/1.207 | 332 | 78.5 | 21.5 | 0.0 |
| 600 | 65 | 1.6947 | 47.69 | 2.009 | −20.13 | 33/2.594 | 32/1.232 | 332 | 81.5 | 18.5 | 0.0 |
| 750 | 65 | 1.7921 | 50.77 | 2.406 | −21.76 | 33/2.701 | 32/1.284 | 332 | 95.4 | 4.6 | 0.0 |

**Time-gate exits (all losers by construction — the gate only fires when open P&L<0):**

| T_gate | time_gate count | time_gate PF | time_gate mean PnL% |
|--------|-----------------|--------------|---------------------|
| 300 | 24 | 0.0000 | −5.842 |
| 400 | 22 | 0.0000 | −7.426 |
| 500 | 14 | 0.0000 | −7.853 |
| 600 | 12 | 0.0000 | −7.547 |
| 750 | 3 | 0.0000 | −4.524 |

**Charts (chart-first):**
- [pf_cvar_pnl_vs_tgate.html](charts/pf_cvar_pnl_vs_tgate.html) — Chart 1: PF / mean PnL / CVaR5 vs T_gate
- [session_pf_vs_tgate.html](charts/session_pf_vs_tgate.html) — Chart 2: RTH PF vs pre-market PF vs T_gate
- [exit_reason_vs_tgate.html](charts/exit_reason_vs_tgate.html) — Chart 3: exit-reason composition vs T_gate

---

## T2 — Direct verdict

**1. Is T_gate=500 still reasonable? No — it is the *worst* value in the swept range.**
PF vs T_gate is **non-monotone (U-shaped) with its trough at 500** (PF: 1.914 → 1.641 →
**1.557** → 1.695 → 1.792). Both ends beat the middle. T500 also has only mid-pack CVaR5
(−21.18) and the weakest RTH PF (2.074). The 500s value — chosen on the retired MDR sample —
does not survive re-validation on the stratified val_r4.

**2. Does a different value materially improve PF / CVaR5 / pre-market? Yes — T_gate=300
dominates on all three, plus both sessions.**
- PF **1.914** (+0.357 vs T500, best of sweep)
- CVaR5 **−14.53%** (+6.65pp vs T500; the **only** value better than the −15% reference line)
- pre-market PF **1.394** (+0.187 vs T500, best of sweep) and RTH PF **2.886** (best of sweep)
- mean PnL 2.369% vs T500's 1.823%

The mechanism is a pure loss-cut: tighter T_gate cuts more losers sooner (24 cuts at −5.84%
mean at T300 vs 14 at −7.85% at T500), which **caps loss depth** — hence best CVaR5 and best PF
despite the **lowest win rate** (44.62%, because more marginal trades are booked as small losses).
This is the expected trade: T300 sacrifices a few percentage points of WR to remove the deep-tail
round-trips. It directly supports the tail-risk-phase hypothesis that cutting pre-market losers
faster (before they round-trip to the ≈−2.7% loser-mode) helps pre-market — pre-market PF is
highest at T300 — though pre-market PF stays modest (~1.4); the time gate helps, it does not fix
pre-market on its own. [[project-phase-state]]

**3. At what T_gate does the time gate become the binding exit? At every tested value.**
`time_gate` share rises monotonically as the gate tightens: 4.6% (T750) → 18.5% → 21.5% →
33.9% → **36.9% (T300)**. It is never 0% — the escalation "cannot bind under any value" condition
is not met; no wiring issue. `session_end` is 0% everywhere (EPG close or the time gate always
resolves the position). At T750 the gate barely fires (3 trades) and the run converges toward the
no-time-gate R1-Final p80 baseline (PF 1.792 ≈ 1.756).

**No selection made — Cooper picks the T_gate value.** The data points at 300; 500 is not defensible.

---

## Escalation Check

| Condition | Threshold | Result |
|---|---|---|
| n_trades at any T_gate < 30 | — | **CLEARED** — n=65 at every value |
| `time_gate` at 0% across every swept value (incl. 300) | — | **CLEARED** — fires at all values (36.9%→4.6%); no wiring issue |

No hard stops triggered.

---

## Output Files

| File | Description | Status |
|---|---|---|
| `sweep_table.md` | T1 full sweep table | ✅ |
| `charts/pf_cvar_pnl_vs_tgate.html` | Chart 1 | ✅ |
| `charts/session_pf_vs_tgate.html` | Chart 2 | ✅ |
| `charts/exit_reason_vs_tgate.html` | Chart 3 | ✅ |
| `sweep_rows.json` | machine-readable per-T_gate metrics | ✅ |
| `summary.md` | T2 verdict + phase summary | ✅ |

Per-run outputs: `tg{300,400,500,600,750}/` (run_summary.json, per_trade.json, per_event_summary.json).

---

## Approval Gate

Do not begin the portfolio/slippage/fee modeling phase or any parameter-robustness / Monte
Carlo work until Cooper reviews this sweep and selects a T_gate value.

**Reproduce:** `scripts/run_r1_5_final_sweep.py` (runs), `scripts/build_r1_5_final_report.py`
(table + charts).
