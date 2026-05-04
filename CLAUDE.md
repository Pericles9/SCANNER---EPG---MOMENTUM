# CLAUDE.md — scanner-epg-momentum

## What This Is

Standalone backtest project for the simplified **Scanner × EPG × LULD** momentum strategy.
Derived from `hawkes-ofi-impact` (Phase S/T/U). Removes the full OFI/price-impact/regime stack.
Entry: Setup Filter PASS + EPG rising edge + gap ≥ 30%.
Exit: EXIT_D (Hawkes intensity imbalance timer) > LULD proximity > EPG window close.

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
- **Test before running backtests.** `pytest tests/ -v` must pass 49/49 before any run.

---

## Current Project State

| Phase | Status | Notes |
|-------|--------|-------|
| Bootstrap | **Complete** | All imports verified, 49/49 tests pass, smoke test OK |
| Phase S baseline | Derived | PF=1.2709, 345 trades, 81 events (100-event val seed=42). See parent. |
| Phase T EXIT_D tuning | Derived | Best: theta=0.65 tau=4s (T10 sweep). See parent. |
| Phase U EXIT_D+LULD integration | Derived | PF=1.0962 (default theta=0.75). Pre-market regression. See parent. |

**What's next:** Re-run Phase U equivalent with theta=0.65/tau=4s across full val split.

---

## Entry Stack

```
Setup Filter (4-signal composite: range, volume, thinness, body conviction)
    ↓ PASS
EPG rising edge (k=5, tau=300s, p=0.65, warmup=300s)
    ↓ PASS AND gap ≥ 30% (backtest: intraday_pct)
ENTRY (LONG)
```

## Exit Stack (first wins)

1. **EXIT_D** — Hawkes intensity imbalance timer: I(t) = λ_sell/(λ_buy+λ_sell) > theta
   for τ_min continuous seconds. Disabled if I_entry > theta (already imbalanced at entry).
2. **LULD proximity** — Price within 2% of Tier 2 LULD band. RTH only (09:30–16:00 ET).
3. **EPG window close** — EPG transitions PASS → FAIL/INACTIVE.

**Config:** `config/strategy.json` (theta=0.65, tau_min=4s from T10 best combo)

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

## Known Issues (from parent project)

1. **Pre-market regression:** Phase U PF dropped 1.73→0.90 for pre-market events with
   EXIT_D+LULD active. Cause unknown. Possible: EXIT_D fires prematurely in thin pre-market
   order flow, or LULD firing pattern differs.
2. **Gap gate queue behavior:** 40 queued entries in Phase U 100-event run; quality unknown.
   These are entries where gap < 30% at EPG fire but later reaches 30%.
3. **EPG one-trade-per-window:** After exit mid-PASS, `prev_state=PASS` means next tick is
   not a rising edge. Maximum one trade per PASS window by design.
4. **T10 best combo not yet validated on full val:** theta=0.65 tau=4s tested on 100-event
   sample only. Full val run pending.

---

## File Naming Conventions

**Results:** `results/{run_name}/`
**Logs:** `logs/{phase_name}_{YYYYMMDD_HHMMSS}.log`
**Config backups:** `config/{filename}_{descriptor}.json`
