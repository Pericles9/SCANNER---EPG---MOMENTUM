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
- **Test before running backtests.** `pytest tests/ -v` must pass all tests before any run. Current count: 378 (grown from initial 49 as phases added new modules and tests).

---

## Current Project State

| Phase | Status | Notes |
|-------|--------|-------|
| Bootstrap | **Complete** | All imports verified, 357 tests pass, smoke test OK |
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
| Phase CPD-0 — PELT calibration | **Complete (Gate-1 approved)** | Log-ratio WJI_log=log(WJI) (background≡1.0); active-seconds axis. Symmetry OK (skew +0.227), σ_log median 0.209. See `results/phase_cpd/cpd0/`. |
| Phase CPD-1 — CUSUM gate | **HARD STOP (T6c)** | `gate_mode="cusum"` added (13/13 tests). 28-config sweep: all fail CVaR5≥−10% (best −30.55%, k12_h8). PF/EV positive everywhere; tails from slow gate-close exit through regime collapse. See `results/phase_cpd/cpd1/`. |
| Phase CPD-DIAG — k12_h8 charts | **Complete** | 15 diagnostic charts (seed=7). 6/15 zero-trade, 2/15 tail events drive the −30% CVaR5. Diagnostic only. See `results/phase_cpd_diag/`. |
| Phase CPD-EXIT — Exit mechanism sweep | **Complete (no full run)** | 3 sub-phases on BOCPD winner entry gate (lh0.01, pe0.6). **Sub-1 TP/SL wins**: tp5_sl5 PF=1.399, CVaR5=−9.14%, EV=0.505. **Sub-2a WJI trailing**: HARD STOP (best CVaR5=−16.83%, raw_pe20). **Sub-2b momentum drop**: best mom6 PF=1.142, CVaR5=−12.60% — below Sub-1. **Sub-3 combined**: best pe10_mo6 PF=1.128, CVaR5=−12.60% — strictly worse than both individual mechanisms. No exit mechanism beats Sub-1. Full val skipped — no actionable improvement. See `results/phase_cpd_exit/`. |
| Phase LULD-REBUILD — Quote-based LULD exit | **T6 pending winner selection** | Replaced trade-price LULD with sticky-ref + NBB bid signal. T4 baseline (thresh=0.02): overall PF=2.2766 (−0.021 vs Phase F), luld_upper PF=146.80 (vs 13.47), n=39 fires. T5 sweep [0.005–0.040]: best overall PF=2.3809 at thresh=0.005. Cooper must select winner before T6 per-event charts. See `docs/Phase_LULD_REBUILD_Results.md`. |
| Phase LULD-V3 — Pin+duration clock | **T6 blocked: Cooper must select winner** | Added `luld_exit_duration_sec` pin+duration clock to proximity exit. Confusion-matrix scoring against halt labels: 1 halt detected (IDAI) across 100-event val sample. Band mismatch finding: halt labeler (30s VWAP) vs module (5-min sticky ref) produce different bands → IDAI generates 0 fires, recall=0.0 for all configs. T5 sweep [0–12s]: n_fires 888→67, composite plateau at 10–12s (−6.38/−6.33). Cooper must select `luld_exit_duration_sec` from T5 table. See `docs/Phase_LULD_V3.md`. |
| Phase LULD-V3b — Band reconciliation + T6 re-score | **HARD STOP (T6)** | T5 fixed band-definition mismatch (30s VWAP→5-min mean + 1% sticky; max divergence was 35.80%). Re-labeled 100-event sample: 32 halts across 18 events (vs 1 in V3). T6 duration sweep [0–12s]: max recall=0.1250 (4/32 halts caught at dur=12s), min mean_liq_penalty=34.589. Both hard-stop criteria triggered. FP rate ≈1.0 at all configs; module fires on band-approach events that don't lead to halts. See `results/phase_luld_v3b/t6_summary.md`. |
| Phase LULD-V3c — Audit + corrected re-score | **HARD STOP (T5a recall)** | Audit found the V3b T6 numbers were instrumentation bugs, not a bad exit. **T2:** pin clock SOUND (0–2 flicker transitions, 83–100% pinned) — not the defect. **T3 (fixed):** halt anchored at `seg_end`; scorer matched only the 15s before it, but exit fires during the run-up ≥15s earlier → 0 TP structurally. Labeler now records `limit_state_start`; scorer matches `[onset−15s, seg_end]`. **T4 (fixed):** liquidity penalty was raw `spread_bps` (range [0,526]) dominating composite; normalized to [0,1] (`/100`). 381 tests pass. Corrected sweep: recall 0.0→0.25, mean_liq 34–45→0.18–0.31, composite −38→−0.20. **T4 liquidity hard-stop CLEARED; T3 recall hard-stop TRIGGERED (max 0.25 < 0.70).** FN diagnostic: genuine, not a 3rd bug — widening lead window 15→300s lifts recall only to 0.31; 22/32 halts have no fire within 300s (6 events zero fires). Root cause: trade-vs-quote divergence — labeler detects halts on trade-price≥band, exit fires on NBBO-bid within 1% of band; bid evaporates during limit-up so the quote exit is blind to ~70% of trade-based halts. Part B (liquidity-adaptive tiers) NOT started per hard-stop. See `results/phase_luld_v3c/`. |
| Phase EPG-Rapid C1 | **Complete** | rho_fast param + entry_eligible(). 307 tests pass. Committed b420163. |
| Phase EPG-Rapid C2 | **DROPPED** | LULD exit infeasible (V3c recall hard-stop). |
| Phase EPG-Rapid C3 | **Complete** | Halt-gap clock pause in `_hawkes_replay_with_refit`. Inter-trade gap spanning a halt window > 60s → `dt_effective=1e-6` (prevents Hawkes intensity decaying to near-zero across trading halts). Committed 7d11964. |
| Phase EPG-Rapid C4 | **Complete** | RocBuffer — 5-min rolling ROC per ticker injected into entry context. Committed b034ecf. |
| Phase EPG-Rapid R0 | **Complete** | EPG-Rapid runner baseline. T2: runner_rapid.py with classic parity mode. T3: parity diff (rapid vs classic). T4: baseline rapid run — n=83, PF=2.077, CVaR5=−17.58%, win%=50.6. T5: per-event HTML charts (GateState.name fix). Exit: EPG PASS→FAIL only. See `results/phase_r0/`. |
| Phase EPG-Rapid R1 — T1 symmetric sweep | **Complete** | cross_and_hold entry (n_hold=3), p ∈ {0.50–0.75} symmetric sweep on 81 val events. Best PF: p0.70 (PF=1.515, CVaR5=−17.86%); p0.65 chosen for diagnostics (PF=1.378, CVaR5=−16.92%, lag=3850s, chatter=11.6). All exits epg_window_close=100%. Pre-market PF=0.67 vs RTH PF=3.11. See `results/phase_r1/symmetric_sweep.json`. |
| Phase EPG-Rapid R1 — D diagnostic | **Complete** | 81 Plotly HTML charts (4-panel: candlestick, E(t), Q̃, gate state) + `entry_lag_diagnosis.json`. **H1 (EventAnchor fires after scanner): 34.6% of events — minority issue; median offset −173.7s (anchor typically fires before scanner).** **H2 (Q̃ smoothing delay): NOT binding — median first_3consec_offset=299.9s (fast qualifying).** **Gate chatter = binding constraint:** every event has ≥2 PASS→FAIL transitions (range 2–35), median entry_lag=2753s, p90=14802s. Root cause: simultaneous gate PASS + Q̃ qualifying required; chatter delays that conjunction. Charts committed 98d6889. |
| Phase EPG-Rapid R1 — T2 asymmetric sweep | **Complete** | 8 configs p_close < p_open. Chatter dropped from 11.6 → 3.7–5.9 mean transitions. Entry lag barely moved (3590–3900s). Selected configs for T3: po70_pc65 (EV=1.41%, PF=1.4109) and low_close_0.40 (EV=2.13%, PF=1.5508). See `results/phase_r1/asymmetric_sweep.json`. |
| Phase EPG-Rapid R1 — T3 per-event charts | **Complete** | 4-panel Plotly HTML charts for both selected T2 configs. po70_pc65: 81 charts. low_close_0.40: 82 charts. Committed d35d9ee. See `results/phase_r1/t3_charts_*/`. |
| Phase R0 Rebuild — first_pass entry | **Complete** | T2 baseline (rising_edge): n=83, PF=2.077, CVaR5=−17.58%, lag=353s. T3 rapid (first_pass): identical results. Finding: first_pass ≡ rising_edge by construction — both fire on first PASS tick. vs old cross_and_hold lag=3850s: 10.9× faster. Gap gate regression found and fixed (was blocking ~half of entries). T6 per-event charts generated. See `results/phase_r0/summary.md`. |

**What's next (EPG-Rapid):** R0 Rebuild complete. first_pass entry confirmed SF-free, lag=353s (10.9× faster than old cross_and_hold). Await Cooper direction on R1 scope — options: (a) threshold sweep (p_open/p_close) on first_pass entry to optimize PF, (b) full-val run on first_pass to confirm out-of-sample edge, (c) proceed directly to R3/R5.

**LULD exit — ABANDONED (decision 2026-06-20).** The quote-based LULD proximity exit line (V3 → V3b → V3c, and LULD-REBUILD) is **closed.** Rationale: a quote-proximity exit is structurally a *Limit-State* detector, but the halt population on these low-float momentum names is dominated by **discretionary Straddle-State pauses** — the NBBO gaps *away* from the band (so there is nothing at the band to detect) and the listing exchange declares the pause at its **discretion** (so there is no deterministic precursor). V3c fixed the two real instrumentation bugs (T3 onset-anchor, T4 penalty units) and verified that the residual recall (~0.25, uplift to only 0.31 even with a 300s lead window) is genuine and untunable. Do not pursue LULD-V3b T6, LULD-REBUILD T6, V3c Part B (liquidity-adaptive tiers), or V3c T1 charts. Full decision record: `docs/Phase_LULD_V3c.md`. The Phase F config retains LULD upper-band only in the frozen legacy lineage; this decision means no further investment, not forced removal from Phase F artifacts.

**Other open context:** Phase H requires explicit approval before any implementation. **Phase G v1/v2 findings (rank gate, heat gate, quartile gate, multi-day runner) are analysis-only and NOT actionable** — the quartile boundary in particular looks good theoretically but breaks down in practice. Do not implement any of these from Phase G without a dedicated validation phase. SlopeGate F_ss is active live but has no backtest validation — the backtest still uses ParticipationGate. Phase CPD-EXIT finding: BOCPD winner entry gate + fixed TP/SL (5%/5%) produces PF=1.399 on val sample but does not exceed the Phase D baseline (PF=2.65) — the BOCPD entry gate is the binding constraint, not the exit mechanism.

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
   **ABANDONED as a research line (2026-06-20)** — structurally a Limit-State detector, blind
   to the discretionary Straddle-State halts that dominate this universe. See
   `docs/Phase_LULD_V3c.md`. Retained only in the frozen Phase F legacy config; not used in
   EPG-Rapid (exit = EPG PASS→FAIL only).
3. **EPG window close** — EPG transitions PASS → FAIL/INACTIVE.

**Config:** `config/strategy.json` — EXIT_D currently **disabled** (`enabled: false`); code retained. LULD upper band active in the frozen Phase F config only; no further LULD development (see decision above).

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
