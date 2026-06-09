# CLAUDE.md — scanner-epg-momentum

## What This Is

Standalone backtest project for the simplified **Scanner × EPG × LULD** momentum strategy.
Derived from `hawkes-ofi-impact` (Phase S/T/U). Removes the full OFI/price-impact/regime stack.
Entry: EPG rising edge + gap ≥ 30% (backtest). SF computed but not a first-entry gate.
Exit: EPG window close (primary). EXIT_D currently disabled. LULD upper band active.

**Source project:** `D:\Trading Research\hawkes-ofi-impact`

This project is intentionally lean. Do not import the OFI normalization, Gate 3, or dynamic-stop
modules from the parent project without explicit approval. The strategy spec is this file +
`docs/Scanner-EPG-Momentum.md`.

---

## Non-Negotiable Standards

Inherited from the parent project — apply here without exception:

- **Online refitting is mandatory.** `_hawkes_replay_with_refit()` must execute during every
  backtest run. A constant `n_base` across all trades is proof the runner is broken.
- **EPG lambda_ref source is mu_buy + mu_sell only.** Not equilibrium rate, not empirical.
  See parent project CLAUDE.md for full rationale.
- **Do not touch the test split.** `config/holdout_boundary.json` is locked.
- **Test before running backtests.** `pytest tests/ -v` must pass all tests before any run. Current count: 152 (grown from initial 49 as phases added new modules and tests).

---

## Current Project State

| Phase | Status | Notes |
|-------|--------|-------|
| Bootstrap | **Complete** | All imports verified, 152 tests pass, smoke test OK |
| Phase S baseline | Derived | PF=1.2709, 345 trades, 81 events (val-sample seed=42). See parent. |
| Phase T EXIT_D tuning | Derived | Best: theta=0.65 tau=4s (T10 sweep). See parent. |
| Phase U EXIT_D+LULD | Derived | PF=1.0962 (theta=0.75). Pre-market regression. See parent. |
| Phase B — Re-entry | **Complete** | PF=1.3825, 1,689 trades, EXIT_D T10 best + re-entry enabled. Pre-market recovered. |
| Phase C — Backside filters | **Complete** | Gap gate disabled. Watermark 5%: PF=1.9443. CVD fixed: PF=1.7544. |
| Phase C.5 — CVD bug fix | **Complete** | Buggy accumulator (ambiguous→sell) found and fixed. Original CVD PF=2.0328 invalid. |
| Phase D — Watermark | **Complete** | Intra-window rolling high watermark. Best 2%: PF=2.6529, n=483. Phase D baseline. |
| Phase E — Symmetric LULD | **Complete** | Spread-multiple LULD both bands. Best N=1: PF=1.9271. Escalation triggered (<2.20). |
| Phase F — Asymmetric LULD | **Complete** | Upper band only. Val-full PF=1.9194, test PF=2.1849. Below Phase D baseline. |
| Phase G — Scanner context | **Complete** | Analysis only. Rank 1 underperforms (PF=1.18). Heat/multi-day runner signals found. |
| Phase G v2 — Momentum quartile | **Complete** | Analysis only. Q4 (secondary movers) PF=3.06 vs Q1 (dominant) PF=1.25. **Quartile gate NOT actionable — breaks down in practice. Do not implement.** |
| Phase EPG-GRT | **Complete** | Gate reaction time sweep. Asymmetric hysteresis wins. Best val: var_a_t300_po65_pc30 PF=2.584. |
| Phase EPG-OPT2 | **Complete** | Stage 1-3 sweep. All below GRT baseline. T8 escalation. SlopeGate F_sl inconclusive. |
| Phase EPG-OPT2-SF | **Complete** | SF integration test. Net negative: mean delta_pf = −0.085. 47/52 configs hurt. |
| Live SlopeGate swap | **Deployed (heuristic)** | Live EPG core: ParticipationGate → SlopeGate F_ss (s3_fss_t180_l30_ko5_kc0). EXIT_D+LULD disabled live. |
| Phase WJI-SlowEMA | **Parked (T3b escalation)** | Slow EMA of WJI as gate reference. All 25 configs fail CVaR5 ≥ −10% (best −16.79%). Root cause: EMA chases signal down during deceleration — late exits, deep tails. T4/T5/T7 blocked. TBD whether to retry or abandon. |

**What's next:** Phase H requires explicit approval before any implementation. **Phase G v1/v2 findings (rank gate, heat gate, quartile gate, multi-day runner) are analysis-only and NOT actionable** — the quartile boundary in particular looks good theoretically but breaks down in practice. Do not implement any of these from Phase G without a dedicated validation phase. SlopeGate F_ss is active live but has no backtest validation — the backtest still uses ParticipationGate.

---

## Entry Stack

```
Scanner (todaysChangePerc ≥ 30%)
    ↓
EPG rising edge (k=5, tau=300s, p=0.65, warmup=300s)
    ↓ PASS AND gap ≥ 30% (backtest: intraday_pct)
ENTRY (LONG)
Re-entry: EPG rising edge AND setup_filter.passes == True
```

Setup filter (4-signal composite: range, volume, thinness, body conviction) roles:

- **Removed from initial entry gate.** Computed but does not block first entry.
- **Re-entry gate:** SF must be passing before a re-entry after EXIT_D.
- **Continuous disqualifier (live only):** q̃ < 0.65 for 15 consecutive bars → remove ticker from universe.

## Exit Stack (first wins)

1. **EXIT_D** — Hawkes intensity imbalance timer: I(t) = λ_sell/(λ_buy+λ_sell) > theta
   for τ_min continuous seconds. Disabled if I_entry > theta (already imbalanced at entry).
2. **LULD proximity** — Price within 2% of Tier 2 LULD band. RTH only (09:30–16:00 ET).
3. **EPG window close** — EPG transitions PASS → FAIL/INACTIVE.

**Config:** `config/strategy.json` — EXIT_D currently **disabled** (`enabled: false`); code retained. LULD upper band active, lower band disabled (Phase F config).

---

## Running the Runner

```bash
# Full val run
python -m backtest.runner --split val --config config/strategy.json

# Quick test (N events)
python -m backtest.runner --split val --random-sample 10 --seed 42 --config config/strategy.json

# Single event debug
python -m backtest.runner --split val --ticker AAPL --date 2024-01-15 --config config/strategy.json
```

Always use `D:\Trading Research\.venv\Scripts\python.exe`.

---

## Config Files

| File | Purpose |
|------|---------|
| `config/strategy.json` | All strategy params: EPG, Hawkes, EXIT_D, LULD, gap gate |
| `config/hawkes_params.json` | Phase A iter 7 calibrated params (alpha, mu, beta) |
| `config/epg_params.json` | EPG params with Phase R rationale annotations |
| `config/q_bar_tiers.json` | Q-bar tier boundaries for Lee-Ready classification |
| `config/holdout_boundary.json` | Train/val/test split dates — locked |

---

## Source Documents

### `docs/Scanner-EPG-Momentum.md`
Strategy spec: entry stack, exit stack, parameter rationale, known limitations.

### `docs/Project_Directory.md`
Module map: directory tree, module interfaces, dependencies.

### `MEMORY.md`
Discovered facts, bugs, open questions. Read at session start.

---

## Known Issues

1. **EPG one-trade-per-window:** After exit mid-PASS, `prev_state=PASS` means the next tick
   is not a rising edge. Maximum one trade per PASS window by design.
2. **Pre-market PF below RTH (Phase F val-full):** Pre-market PF=1.497 vs RTH PF=2.279 on the
   full val split. May be period-specific (2023–mid-2024); Phase F test pre-market recovered
   to 2.133.
3. **epg_window_close is near-breakeven on full val:** PF=1.018 (41.99% of trades). Sample
   runs are optimistic for this exit reason. Improving it is a Phase H candidate.
4. **Rank 1 underperformance:** Scanner rank 1 trades PF=1.18 vs ranks 3–9 PF=2.67–6.04
   (Phase G). Observed in analysis; NOT actionable. Phase G v1/v2 findings including rank gate and quartile gate are analysis-only — do not implement without a dedicated validation phase.
5. **Gap gate disabled (Phase C+):** Gap gate removal introduces look-ahead bias vs live
   scanner (which filters by ≥30% gap). Phase C PF uplift vs Phase B is partially from this.
   The intra-window watermark partially mitigates it.
6. **SlopeGate F_ss (live only): no backtest validation.** Deployed in live on 2026-06-03.
   The backtest runner still uses ParticipationGate.

---

## File Naming Conventions

**Results:** `results/{run_name}/`
**Logs:** `logs/{phase_name}_{YYYYMMDD_HHMMSS}.log`
**Config backups:** `config/{filename}_{descriptor}.json`
