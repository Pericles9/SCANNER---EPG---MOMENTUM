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
| Phase CPD-0 — PELT calibration | **Complete (Gate-1 approved)** | Log-ratio WJI_log=log(WJI) (background≡1.0); active-seconds axis. Symmetry OK (skew +0.227), σ_log median 0.209. See `results/phase_cpd/cpd0/`. |
| Phase CPD-1 — CUSUM gate | **HARD STOP (T6c)** | `gate_mode="cusum"` added (13/13 tests). 28-config sweep: all fail CVaR5≥−10% (best −30.55%, k12_h8). PF/EV positive everywhere; tails from slow gate-close exit through regime collapse. See `results/phase_cpd/cpd1/`. |
| Phase CPD-DIAG — k12_h8 charts | **Complete** | 15 diagnostic charts (seed=7). 6/15 zero-trade, 2/15 tail events drive the −30% CVaR5. Diagnostic only. See `results/phase_cpd_diag/`. |
| Phase CPD-EXIT — Exit mechanism sweep | **Complete (no full run)** | 3 sub-phases on BOCPD winner entry gate (lh0.01, pe0.6). **Sub-1 TP/SL wins**: tp5_sl5 PF=1.399, CVaR5=−9.14%, EV=0.505. **Sub-2a WJI trailing**: HARD STOP (best CVaR5=−16.83%, raw_pe20). **Sub-2b momentum drop**: best mom6 PF=1.142, CVaR5=−12.60% — below Sub-1. **Sub-3 combined**: best pe10_mo6 PF=1.128, CVaR5=−12.60% — strictly worse than both individual mechanisms. No exit mechanism beats Sub-1. Full val skipped — no actionable improvement. See `results/phase_cpd_exit/`. |
| Phase LULD-REBUILD — Quote-based LULD exit | **T6 pending winner selection** | Replaced trade-price LULD with sticky-ref + NBB bid signal. T4 baseline (thresh=0.02): overall PF=2.2766 (−0.021 vs Phase F), luld_upper PF=146.80 (vs 13.47), n=39 fires. T5 sweep [0.005–0.040]: best overall PF=2.3809 at thresh=0.005. Cooper must select winner before T6 per-event charts. See `docs/Phase_LULD_REBUILD_Results.md`. |
| Phase SEB — Scanner Entry Backtest | **Complete (harness)** | Read-only causal backtest: scanner → setup filter → first-bar-above-VWAP, exit-agnostic. Tier 0 (live ground truth from scanner_snapshots) + Tier 1 (catalog 7-day window). Causality rules A–F enforced; Gate B assertions in code. Outputs: `results/seb/entries.parquet`, `results/seb/seb_report.md`. Deliverable is the Tier 1 vs Tier 0 runner-rate GAP. See `backtest/tools/seb/`. |
| Phase SEB-X — Vol-normalized exit sweep | **Complete (research)** | Path replay + exit policy sweep on 990 frozen SEB Tier-1 entries. Gate A PASS (MFE reproduced to <7.5% relative). **HEADLINE FINDING: σ degeneration — 99.8% of entries use global-median fallback ($0.44) because armed_bar==entry_bar (1-bar window < 3-bar minimum); per-event vol normalization did not work.** Gate C: INCONCLUSIVE (σ constant → vol-regime split is degenerate; cannot test normalization hypothesis). Recommended stack (UNVALIDATED): B0+R1+R3 k1=2.5σ arm=1.0σ g=0.5σ; confirm-split capture=−0.166 vs B0=−0.439. Year split: 2021–2023 positive (PF 1.06–1.47), 2024 sharply negative (capture=−0.211, PF=0.946 — dominates confirm split). See `backtest/tools/seb_x/`, `results/seb_x/exit_report.md`. |
| Phase SEB-X v2-VIZ — Presentation layer over v2 frozen artifacts | **Complete (research)** | Read-only visualization layer on frozen SEB-X v2 (990 entries × 3 stacks). **Gate A PASS** (MFE medians match reference to <0.01%). **Gate B exit-reason breakdown (B0+R1+R3_vwap)**: R3=57.3% (trail dominant), R1=36.1%, B0=6.3%, horizon=0.4%. **Gate C HEADLINE: B0 is fat-right-tail** — EV=+0.38% vs median=−3.16% (rare big winners lift mean; majority of trades lose). B0+R1+R3_vwap: EV=+1.03%, median=+3.06% — sign-agree, no fat-tail flag. Median capture (robust): B0+R1+R3_vwap=+0.204, prim=+0.158. Capture EV ≈ −7 for all stacks (ratio artifact when MFE≈0; use median). **Gate D**: exit-bar assertion passes all 20 curated charts. Outputs: `results/seb_x_v2viz/` — per_trade_exits.parquet, metrics_v2viz.md/.csv, 10 distribution PNGs, 36 trade PNGs, 2 contact sheets. No new exit logic — presentation only over frozen v2 artifacts. |
| Phase SEB-X v2 — Vol-normalized exit sweep (v2) | **Complete (research)** | Parkinson σ (trailing 20 bars before entry_bar) + ADR floor (c=0.05, T-3..T-1 RTH sessions). σ CV=7.544; 35.4% floor-bound; 166/990 entries had no prior ADR data. **Gate A PASS** (MFE reuse ✓, σ non-degenerate ✓). **Gate C PASS for BOTH σ units: k1 divergence=0.0%** — normalization hypothesis CONFIRMED (k1=1.0 optimal regardless of vol regime). Complexity ladder winner (both units): **B0+R1+R3 k1=2.5 arm=2.0 g=0.5** (conf cap=+0.098 / PF=1.144 under σ_primary; conf cap=+0.152 / PF=1.261 under σ_vwap). **Gate D FLAG: σ_primary 2024 edge decay delta=−0.133** (pre-2024 cap=0.185, 2024 cap=0.052); σ_vwap moderate decay delta=−0.096 (2024 cap=0.144). σ_vwap more robust to 2024 regime shift. CVaR5 in σ-units is not interpretable when σ varies 10× (CV=7.5). **UNVALIDATED — no Tier 0 ground truth; 2024 regime shift must be monitored before deploying.** See `backtest/tools/seb_x_v2/`, `results/seb_x_v2/exit_report_v2.md`. |

**What's next:** Phase LULD-REBUILD T6 requires Cooper to select a winner threshold from the T5 sweep table — see `docs/Phase_LULD_REBUILD_Results.md`. After T6, Phase H requires explicit approval before any implementation. Phase SEB harness is built (`backtest/tools/seb/`) — run it to measure the backtest→live gap: `python backtest/tools/seb/run_seb.py --tier0-db-url <URL>` (or `--tier0-json` for pre-exported snapshots, `--no-tier0` for Tier 1 only). **Phase G v1/v2 findings (rank gate, heat gate, quartile gate, multi-day runner) are analysis-only and NOT actionable** — the quartile boundary in particular looks good theoretically but breaks down in practice. Do not implement any of these from Phase G without a dedicated validation phase. SlopeGate F_ss is active live but has no backtest validation — the backtest still uses ParticipationGate. Phase CPD-EXIT finding: BOCPD winner entry gate + fixed TP/SL (5%/5%) produces PF=1.399 on val sample but does not exceed the Phase D baseline (PF=2.65) — the BOCPD entry gate is the binding constraint, not the exit mechanism. **Phase SEB-X v2 finding: σ normalization CONFIRMED (Gate C PASS, 0.0% k1 divergence across vol regimes). Best stack: B0+R1+R3 k1=2.5 arm=2.0 g=0.5 (conf cap=+0.098 primary, +0.152 vwap). Gate D flags 2024 edge decay (delta=−0.133 primary; −0.096 vwap). σ_vwap more robust: 2024 cap=0.144 vs 0.052 primary. Stack is UNVALIDATED — no Tier 0 ground truth. 2024 regime shift must be monitored before deploying any exit rule.** Run: `python backtest/tools/seb_x_v2/run_seb_x_v2.py --skip-sigma --skip-sweep` to regenerate report. **Phase SEB-X v2-VIZ finding: Gate C headline — B0 is fat-right-tail (EV+0.38% vs median−3.16%); B0+R1+R3_vwap exits agree (EV+1.03%, median+3.06%). Trail (R3) fires 57% of exits — most trades reach arm threshold (entry+2σ). Capture EV ≈ −7 for all stacks (ratio artifact; use median capture: +0.204 vwap, +0.158 prim). This is presentation-only over frozen v2 artifacts.** Run: `python backtest/tools/seb_x_v2viz/run_v2viz.py --skip-trades --skip-metrics` to regenerate charts.

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
