---
tags:
  - type/plan
  - domain/backtest
  - project/scanner-epg-momentum
  - status/proposal
created: 2026-06-18
strategy_id: epg_rapid
companion: EPG_Rapid_Strategy.md
---

# EPG-Rapid — Test Phase Plan

Phased validation for `epg_rapid`. All phases follow `Agent_Prompt_Standard.md` (v1.1).

**Companion documents:**
- `EPG_Rapid_Strategy.md` — strategy spec (read first)
- `LULD_Halt_Architecture.md` — regulatory mechanics of the rebuilt LULD module (read
  before C2 or R4)
- `Phase_LULD_REBUILD.md` — the rebuild's own agent prompt and original upper-only sweep
  (R4 reuses its threshold grid for comparability)

**Core discipline:**

- **Isolation before integration.** Component validation phases (C1–C4) must complete
  before the first backtest runs. Each new code piece is unit-tested in isolation and
  approved before being incorporated into the next.
- **No cross-contamination.** A C-phase failure stops the sequence. Do not proceed to
  integration (R0) until all four C-phases are green.
- **Default sample:** 100-event val, seed=42 (stratified). Full val is a milestone run
  used only in R5. Test split is never touched in C or R phases — opened once at the
  very end of R5.
- **No winner selection by agents.** Agents present data. Cooper selects all swept
  parameters. Algorithmic criteria only permit autonomous selection where explicitly stated.

---

## Phase Map

| Phase | Type | Purpose | Sample | Full val? |
|---|---|---|---|---|
| C1 | Component | `setup_filter` modularity (`rho_fast` param + `entry_eligible`) | Unit tests only | No |
| C2 | Component | **DROPPED** — LULD exit infeasible (V3c recall hard-stop) | Unit tests only | No |
| C3 | Component | Halt-gap clock pause in Hawkes/gate replay | Unit tests only | No |
| C4 | Component | ROC 5-min buffer (per-ticker, partial-window, first-appearance) | Unit tests only | No |
| R0 | Integration | Rapid runner build + parity verification + gate-consistent baseline | 100-event val | No |
| R1 | Tuning | EPG gate threshold (`p_open` × `p_close`) — regime gate recalibration | 100-event val | No |
| R2 | Tuning | Setup filter entry (`rho_fast` × `n_hold`) | 100-event val | No |
| R3 | Tuning | 5-min ROC gate threshold | 100-event val | No |
| R4 | Tuning | **DROPPED** — no LULD module in EPG-Rapid exit stack | — | — |
| R5 | Milestone | Integration + confirmation | Full val → test once (conditional) | **Yes** |

Phases are strictly sequential. Each requires Cooper's explicit approval before the next begins.

---
---

## Phase C1 — setup_filter Modularity

**Objective:** Add `rho_fast` as a runtime parameter to `_compute_setup_signals` and
`run_setup_filter`. Add `entry_eligible()`. Prove the default path is bit-for-bit identical
to the current code.

---

**Context:**
- No backtest run. No event data. Unit tests only.
- Three files must be updated identically: `setup_filter.py` (root),
  `backtest/setup_filter.py`, `backtest/core/filters/setup_filter.py`.
- `_compute_setup_signals` is `@njit(cache=True)`. Adding a parameter invalidates the
  Numba cache. Clear `.numba_cache_hash` files and force recompilation. Confirm it
  re-warms cleanly before running tests.
- `entry_eligible()` is a new standalone function in `backtest/core/filters/rapid_entry.py`.
  No Numba. Reads `result.q_tilde` (confirmed trajectory array, not scalar).

---

## Tasks

- [ ] **T1 — Add `rho_fast` param**
  `_compute_setup_signals(..., rho_fast: float = RHO_FAST)` — default preserves current
  behavior. `run_setup_filter(..., rho_fast: float = RHO_FAST)` — forwards to kernel.
  Applied to all three copies identically.
  - [ ] T1a — Diff all three copies after editing. Must be identical. Post the diff.
  - [ ] T1b — Numba cache cleared and re-warmed without error.

- [ ] **T2 — Add `entry_eligible()`**
  New file `backtest/core/filters/rapid_entry.py`:
  ```python
  def entry_eligible(result: SetupFilterResult, n_hold: int = 3) -> bool:
      q = result.q_tilde
      if len(q) < n_hold:
          return False
      return bool(np.all(q[-n_hold:] >= Q_THRESHOLD))
  ```

- [ ] **T3 — Unit tests**
  - [ ] T3a — All existing setup-filter tests pass unchanged (default `rho_fast` path).
  - [ ] T3b — `run_setup_filter(..., rho_fast=RHO_FAST)` produces identical output to
    `run_setup_filter(...)` on a known event. Use a synthetic tick array if needed.
  - [ ] T3c — `run_setup_filter(..., rho_fast=0.75)` produces different (faster-decaying)
    `q_tilde` values than the default on the same input.
  - [ ] T3d — `entry_eligible(result, n_hold=15)` returns the same True/False as the old
    15-bar sustain for a known synthetic result object where the answer is unambiguous.
  - [ ] T3e — `entry_eligible(result, n_hold=1)` returns True when only the last bar passes.
  - [ ] T3f — `entry_eligible(result, n_hold=3)` returns False when one of the last 3 bars
    is below threshold.

---

## Escalation Criteria

| Condition | Threshold | Action |
|---|---|---|
| Three copies are not identical | any diff | Hard stop — post the diff |
| Any existing test fails | any failure | Hard stop — post failures |
| Numba cache re-warm fails | any error | Hard stop — post error |
| `rho_fast=RHO_FAST` output differs from unparameterized output | any numerical diff | Hard stop |

---

## Output Files

| File | Description |
|---|---|
| `backtest/core/filters/rapid_entry.py` | New `entry_eligible()` module |
| `backtest/tests/test_rapid_entry.py` | Unit tests for `entry_eligible()` |
| `backtest/tests/test_setup_filter_rho_fast.py` | Unit tests for `rho_fast` param |

---

## Reporting

Post: three-way diff result (expect empty), all test results (pass/fail counts), Numba
re-warm confirmation, escalation check table.

---

## Approval Gate

Do not begin C2 until Cooper has reviewed and given explicit approval.

---
---

## Phase C2 — LuldProximityExit Independent Thresholds

**DROPPED 2026-06-20.** LULD exit infeasible after Nasdaq halt system reverse-engineering. Trade-price–based halt detection and quote-based exit signal operate on incompatible axes; structural recall ceiling ~0.31 regardless of lead window. No implementation carried forward into EPG-Rapid. `LuldProximityExit` retained unchanged for classic EPG runner only.

---
---

## Phase C3 — Halt-Gap Clock Pause

**Objective:** Add halt-gap `dt` substitution to `_hawkes_replay_with_refit` so Hawkes
EMA and gate `λ_V` do not decay across detected halt windows. Prove the no-halt path is
unchanged.

---

**Context:**
- No full backtest run. Unit tests with synthetic data.
- File: `backtest/runner.py` — `_hawkes_replay_with_refit`.
- `detect_luld_halts()` in `luld_halt_detection.py` produces `HaltWindow` list (already
  exists). The replay loop receives this list as an optional argument.
- For any adjacent trade pair where the inter-trade gap spans a halt window, substitute
  `dt_effective = 1e-6` instead of the wall-clock `dt`. This prevents the exponential
  decay terms from collapsing intensity and `λ_V` across the suspension.
- **When `halt_windows=None` or `[]`, behavior is 100% identical to current code.** The
  substitution is gated on halt window presence.
- Gate `λ_V` uses `dt` in its own decay. Same substitution applies.

---

## Tasks

- [ ] **T1 — Add `halt_windows` parameter**
  `_hawkes_replay_with_refit(..., halt_windows: list[HaltWindow] | None = None)`.
  Default `None` = current behavior, no substitution.

- [ ] **T2 — Implement `dt` substitution**
  For each trade pair `(t_i, t_{i+1})`, if any `HaltWindow` overlaps the interval
  `(t_i, t_{i+1})` and `t_{i+1} - t_i > halt_gap_threshold` (60s), set
  `dt_effective = 1e-6`. Otherwise `dt_effective = t_{i+1} - t_i` (current).

- [ ] **T3 — Unit tests**
  Construct synthetic trade arrays with a known gap. Run replay with and without
  a `HaltWindow` covering that gap.
  - [ ] T3a — `halt_windows=None`: Hawkes λ and gate `λ_V` at tick N match current
    implementation exactly (numerical identity).
  - [ ] T3b — `halt_windows=[HaltWindow(start=t_gap_start, end=t_gap_end)]`: λ at the
    first post-gap tick is NOT collapsed to near-zero (i.e., the decay was suppressed).
    Quantify: λ_with_pause > λ_without_pause * 10 for a gap of 300s.
  - [ ] T3c — Gate `λ_V` similarly elevated at post-gap tick when halt window present.
  - [ ] T3d — A gap smaller than `halt_gap_threshold` is NOT paused even when halt window
    covers it (avoid over-triggering on legitimate short gaps).
  - [ ] T3e — Multiple halt windows in a single event are handled correctly.

---

## Escalation Criteria

| Condition | Threshold | Action |
|---|---|---|
| `halt_windows=None` path differs from current | any numerical diff (T3a) | Hard stop |
| Any test failure | any | Hard stop — post failures |

---

## Output Files

| File | Description |
|---|---|
| `backtest/runner.py` | Updated `_hawkes_replay_with_refit` with halt_windows param |
| `backtest/tests/test_halt_clock_pause.py` | Unit tests |

---

## Reporting

Post: test results, λ ratio from T3b (quantified), escalation check table.

---

## Approval Gate

Do not begin C4 until Cooper has reviewed and given explicit approval.

---
---

## Phase C4 — ROC 5-Minute Buffer

**Objective:** Implement the per-ticker rolling scanner history buffer and `compute_roc_5m()`
function. Prove correct behavior across full window, partial window, first appearance,
and multi-ticker isolation.

---

**Context:**
- No backtest run. Unit tests with synthetic scanner history data.
- The ROC buffer stores `(timestamp_ns, pct_change)` pairs per ticker. On each scanner
  poll, the buffer is updated and the 5-min ROC is computed by looking back to the nearest
  poll at least 5 minutes old.
- Lives in the backtest snapshot reader (and eventually the live scanner monitor). For now,
  implement as a standalone module: `backtest/core/filters/roc_buffer.py`.
- `NULL` (not `−inf`) stored when ROC is undefined (first appearance). Downstream SQL
  queries on `scanner_roc_5m_at_fire` must handle `NULL`.

---

## Tasks

- [ ] **T1 — Implement `RocBuffer` class**
  ```python
  class RocBuffer:
      def __init__(self, window_sec: float = 300.0):
          ...
      def update(self, ticker: str, ts_ns: int, pct_change: float) -> None:
          ...
      def compute(self, ticker: str, ts_ns: int) -> tuple[float | None, float]:
          # Returns (roc_5m, window_sec_actual)
          # roc_5m = None if no prior poll exists (first appearance)
          # window_sec_actual = actual lookback used in seconds
          ...
  ```

- [ ] **T2 — Unit tests**
  - [ ] T2a — Full window: two polls 310s apart → `roc_5m = pct_2 - pct_1`,
    `window_sec_actual ≈ 310`.
  - [ ] T2b — Partial window: earliest poll is 120s old → `roc_5m = pct_2 - pct_1`,
    `window_sec_actual ≈ 120` (uses partial window, does not block).
  - [ ] T2c — First appearance: no prior poll → `roc_5m = None`, admit (skip ROC gate).
  - [ ] T2d — Multi-ticker isolation: AAPL buffer and TSLA buffer do not bleed into each
    other. Updating AAPL does not change TSLA's compute output.
  - [ ] T2e — Old polls beyond retention window are pruned (buffer doesn't grow unbounded).
  - [ ] T2f — Nearest poll ≥ 5min is used, not the most recent poll (correct direction).

---

## Escalation Criteria

| Condition | Threshold | Action |
|---|---|---|
| Any test failure | any | Hard stop — post failures |
| Multi-ticker bleed (T2d) | any | Hard stop |

---

## Output Files

| File | Description |
|---|---|
| `backtest/core/filters/roc_buffer.py` | `RocBuffer` class |
| `backtest/tests/test_roc_buffer.py` | Unit tests |

---

## Reporting

Post: all test results, escalation check table.

---

## Approval Gate

Do not begin R0 until Cooper has reviewed and given explicit approval.

---
---

## Phase R0 — Rapid Runner Integration & Baseline

**Date:** TBD
**Baseline:** Classic EPG on 100-event val with EPG-Rapid exit stack — PF TBD at run time
**Objective:** Assemble all C-phase components into `runner_rapid.py`, prove parity against
the classic runner, and establish the gate-consistent baseline
**Primary success metric:** Parity diff empty; baseline metrics written

---

**Context:**
- All four C-phases approved. All component unit tests green.
- Val sample: 100 events, seed=42. No full val. No test split.
- ROC gate disabled in R0 (`roc_min = None`). Gate threshold at starting point 0.65/0.65.
- Exit stack: EPG window close only. `LuldProximityExit` not used in EPG-Rapid; EXIT_D disabled.
- Halt windows: `detect_luld_halts()` called per event; passed to replay.
- The classic runner path (`runner.py`) must not be modified. EPG-Rapid is in
  `runner_rapid.py` (or a `--mode rapid` flag). Verify the classic path still runs
  identically to its pre-C-phase state.

---

## Tasks

- [ ] **T1 — Verify C-phase components integrated**
  Confirm all four C-phase deliverables are importable and unit tests still pass in the
  assembled repo. Run full unit test suite. Any failure = stop before building the runner.

- [ ] **T2 — Build `runner_rapid.py`**
  Mirrors `runner.py` structure. Entry path diverges at the point where classic EPG waits
  for FAIL→PASS rising edge:
  - Rapid path: checks `gate.state == GateState.PASS` directly (no rising-edge requirement)
  - Feeds `halt_windows` to `_hawkes_replay_with_refit`
  - Calls `entry_eligible(result, n_hold)` from `rapid_entry.py`
  - Sets `closed_today=True` at entry before fill, hard re-entry off

- [ ] **T3 — Parity check**
  Configure `runner_rapid.py` to mimic classic EPG exactly: `entry_mode=classic`
  (rising-edge), `n_hold=15`, `rho_fast=0.90`, `roc_min=None`, EXIT_D config matching the
  classic baseline, and **`lower_enabled=False`** (classic `runner.py`'s locked config —
  per `backtest/CLAUDE.md` — is upper-only). Run on 100-event val. Produce trade-level
  JSON diff vs `runner.py` on same events.
  - [ ] T3a — Diff is empty. Any non-empty diff = hard stop.

- [ ] **T4 — Gate-consistent baseline run**
  Classic entry (rising-edge + 15-bar sustain), EPG-Rapid exit stack (EXIT_D off, EPG
  close on). This is the comparator for R1–R3. Write `results/phase_r0/baseline_metrics.json`.

- [ ] **T5 — Per-event charts**
  Standard 4-panel per traded event (Agent_Prompt_Standard §7).
  - Panel 1: 10s candlesticks, entry/exit markers
  - Panel 2: I(t) = λ_sell / (λ_buy + λ_sell), theta line. No EXIT_D markers.
  - Panel 3: Setup filter — `q_tilde` trajectory, 0.65 threshold line, entry-eligible bars shaded green. Step-function, one value per 1-min bar. Scale 0–1.
  - Panel 4: EPG gate state (PASS / FAIL / WARMUP) as colored bands
  - [ ] T5a — Charts for all traded events
  - [ ] T5b — `results/phase_r0/event_charts/index.html` — sortable by ticker, date,
    session_bucket, n_trades, event_pf, exit_reason, mean_hold_sec

---

## Escalation Criteria

| Condition | Threshold | Action |
|---|---|---|
| Any C-phase unit test fails post-integration (T1) | any failure | Hard stop — post which test |
| Parity diff non-empty (T3a) | any diff | Hard stop — post the diff |
| Baseline PF (T4) | < 1.30 | Hard stop — baseline too weak to compare against |

---

## Output Files (committed with `git add -f` as needed)

| File | Commit? |
|---|---|
| `backtest/runner_rapid.py` | Yes |
| `results/phase_r0/baseline_metrics.json` | Yes (`-f`) |
| `results/phase_r0/exit_breakdown.json` | Yes (`-f`) |
| `results/phase_r0/parity_diff.json` | Yes (`-f`) |
| `results/phase_r0/summary.md` | Yes |
| `results/phase_r0/event_charts/*.html` | No — local only |
| `results/phase_r0/event_charts/index.html` | No — local only |

---

## Reporting

Post: parity result, baseline metrics table, exit breakdown, escalation check table,
output file table.

---

## Approval Gate

Do not begin R1 until Cooper has reviewed and given explicit approval.

---
---

## Phase R1 — EPG Gate Threshold Tuning

**Date:** TBD
**Baseline:** R0 baseline metrics
**Objective:** Recalibrate `p_open` and `p_close` for EPG-Rapid's role (primary exit
driver, no EXIT_D backing). Find the threshold that most cleanly detects regime state
without chattering.
**Primary success metric:** A presented sweep table with chatter and PF metrics across
all configs; Cooper selects

---

**Context:**
- Val sample: 100 events, seed=42. No full val. No test.
- `p_open` and `p_close` are not PF-optimization targets. The regime gate should correctly
  identify when momentum is active vs exhausted. PF is a downstream consequence.
- Entry config: cross-and-hold at `rho_fast=0.90, n_hold=3` (default — not yet tuned).
  ROC disabled. LULD at starting thresholds (rebuilt module). These are held fixed to isolate the gate variable.
- Key diagnostics: **gate chatter** (mean PASS→FAIL transitions per trade — low = clean
  regime detection) and **exit-reason distribution** (what fraction of exits are EPG close
  vs other). Both reported per config.
- Asymmetric configs: `p_open > p_close` = enters on stronger signal, exits faster;
  `p_open < p_close` = enters more easily, holds through pullbacks.

---

## Tasks

- [ ] **T1 — Symmetric sweep**
  `p_open = p_close ∈ {0.50, 0.55, 0.60, 0.65, 0.70, 0.75}`. Six configs.
  Per config: PF, n_trades, win%, mean_pnl%, CVaR5, mean_hold_sec, mean_entry_lag,
  mean_passtofail_per_trade (chatter), exit_reason_distribution.

- [ ] **T2 — Asymmetric sweep (staged)**
  Run only after T1 results are posted and Cooper confirms asymmetric is worth exploring.
  If Cooper says run it: `p_open ∈ {0.60, 0.65, 0.70}` × `p_close ∈ {0.55, 0.60, 0.65, 0.70}`,
  excluding symmetric arms already in T1. Twelve additional configs.

- [ ] **T3 — Chatter diagnostic**
  For each config, histogram of PASS→FAIL transitions per trade. Flag any config where
  >20% of trades have ≥3 transitions (chattering gate).

- [ ] **T4 — Per-event charts**
  Standard 4-panel (same as R0 layout) for the symmetric arm that Cooper flags after
  seeing T1. Do not pre-select. If Cooper flags nothing, chart `p=0.65/0.65` (starting
  point arm) and note final-config charts follow selection. Panel 4 gate-state coloring
  is the key visual here — should show regime transitions clearly.
  - [ ] T4a — Charts for flagged config(s)
  - [ ] T4b — Sortable index

---

## Escalation Criteria

| Condition | Threshold | Action |
|---|---|---|
| Best symmetric CVaR5 | < −15% | Hard stop — post table |
| All symmetric configs PF | < R0 baseline PF | Hard stop — gate recalibration offers no improvement |
| Chatter rate > 20% at all thresholds | — | Flag — gate may be unsuitable as primary exit; post diagnostic |

---

## Output Files

| File | Commit? |
|---|---|
| `results/phase_r1/symmetric_sweep.json` | No — local only |
| `results/phase_r1/asymmetric_sweep.json` | No — local only (if run) |
| `results/phase_r1/chatter_diagnostic.json` | No — local only |
| `results/phase_r1/summary.md` | Yes |
| `results/phase_r1/event_charts/*.html` | No — local only |
| `results/phase_r1/event_charts/index.html` | No — local only |

---

## Reporting

Post: symmetric sweep table sorted by PF, chatter diagnostic table, exit-reason breakdown
per config, escalation check table. No winner recommendation.

---

## Approval Gate

Do not begin R2 until Cooper has selected `p_open`/`p_close` and given explicit approval.

---
---

## Phase R2 — Setup Filter Entry Tuning

**Date:** TBD
**Baseline:** R0 baseline (classic entry) — PF/lag from R0
**Objective:** Tune the cross-and-hold entry (`rho_fast` × `n_hold`) against the gate
threshold locked in R1. Quantify PF and entry-lag improvement vs 15-bar sustain.
**Primary success metric:** At least one config beats R0 baseline PF with materially lower
mean entry lag

---

**Context:**
- Gate threshold: Cooper's R1 selection (locked).
- ROC disabled. LULD at starting thresholds (rebuilt module). Only entry params vary.
- Catalog bias: cross-and-hold enters earlier and therefore admits earlier fader entries
  that the sustain would block. The event catalog is hindsight-clean and doesn't contain
  faders. This understates real-world false-entry cost. State in results.
- `rho_fast=0.90, n_hold=15` is included as the explicit classic-sustain reference arm.

---

## Tasks

- [ ] **T1 — Sweep**
  `rho_fast ∈ {0.70, 0.75, 0.80, 0.90}` × `n_hold ∈ {1, 2, 3, 5}` + classic reference
  arm (`rho_fast=0.90, n_hold=15`). 17 configs. Per config: PF, n_trades, win%,
  mean_pnl%, CVaR5, mean_hold_sec, mean_entry_lag_sec, blocked_rate_by_reason.

- [ ] **T2 — Entry-lag analysis**
  Distribution of `entry_lag_sec = t_entry − t_scanner_hit` per config. Report median
  and p90 lag vs the classic-sustain arm.

- [ ] **T3 — Fader-proxy diagnostic**
  Per config, count entries that exited at a loss within the first
  `mean_hold_sec` (from the classic-sustain arm). Reports as a fraction of total entries.

- [ ] **T4 — Per-event charts**
  Charts for two configs Cooper flags after seeing T1. If nothing flagged, chart
  `rho_fast=0.75, n_hold=3` and the classic-sustain arm as documented defaults.
  Same 4-panel layout as R0/R1.
  - [ ] T4a — Charts written
  - [ ] T4b — Sortable index

---

## Escalation Criteria

| Condition | Threshold | Action |
|---|---|---|
| Best-config CVaR5 | < −15% | Hard stop |
| All configs PF | ≤ R0 baseline PF | Hard stop — cross-and-hold adds no value |
| Any config n_trades | < 30 | Flag (not hard stop) — mark unreliable in table |

---

## Output Files

| File | Commit? |
|---|---|
| `results/phase_r2/summary.md` | Yes |
| `results/phase_r2/sweep_results.json` | No |
| `results/phase_r2/entry_lag.json` | No |
| `results/phase_r2/fader_proxy.json` | No |
| `results/phase_r2/event_charts/*.html` | No |
| `results/phase_r2/event_charts/index.html` | No |

---

## Reporting

Post: sweep table sorted by PF, lag-reduction table vs classic-sustain, fader-proxy table,
escalation check table. No winner recommendation. Include catalog-bias caveat verbatim.

---

## Approval Gate

Do not begin R3 until Cooper has selected `rho_fast`/`n_hold` and given explicit approval.

---
---

## Phase R3 — 5-Minute ROC Gate Tuning

**Date:** TBD
**Baseline:** R2-selected entry config with ROC disabled
**Objective:** Determine whether the 5-min ROC gate adds selection value and at what threshold
**Primary success metric:** Show whether any `roc_min` arm improves PF and/or CVaR5 vs
the disabled arm without unacceptable trade-count loss

---

**Context:**
- Gate threshold: R1 selection. Entry config: R2 selection. Both locked.
- ROC computed from `RocBuffer` (C4). Scanner history at actual poll cadence (15–30s).
- First appearance = admit (skip ROC). Partial window allowed, actual lookback recorded.
- `NULL` stored (not `−inf`) for first-appearance and disabled arms.

---

## Tasks

- [ ] **T1 — ROC computation**
  Compute `roc_5m` and `window_sec_actual` per event at scanner-fire time using `RocBuffer`.
  Store `scanner_roc_5m_at_fire` and `scanner_roc_window_sec_actual` in sessions output.
  Disabled arm stores `NULL`.

- [ ] **T2 — Sweep**
  `roc_min ∈ {disabled, 0.05, 0.10, 0.15, 0.20, 0.25}`. Per arm: PF, n_trades, CVaR5,
  n_blocked, n_first_appearance_skip, n_partial_window.

- [ ] **T3 — Selection-value analysis**
  For each threshold: PF and CVaR5 of admitted vs blocked sets. Are blocked entries
  systematically worse (ROC has edge) or random (ROC just cuts volume)?

- [ ] **T4 — Partial-window sensitivity**
  Split results by full-window vs partial-window entries. Confirm partial-window entries
  are not driving any apparent effect.

- [ ] **T5 — Per-event charts**
  ROC-disabled arm + one Cooper-flagged threshold arm. `roc_5m` value annotated on entry
  marker. Same 4-panel layout. Sortable index.
  - [ ] T5a — Charts written
  - [ ] T5b — Sortable index

---

## Escalation Criteria

| Condition | Threshold | Action |
|---|---|---|
| Best ROC arm CVaR5 | < −15% | Hard stop |
| Tightest arm (0.25) n_trades | < 20 | Flag — mark unreliable |
| Blocked-set PF ≥ admitted-set PF at every threshold | — | Hard stop — ROC is anti-selective |

---

## Output Files

| File | Commit? |
|---|---|
| `results/phase_r3/summary.md` | Yes |
| `results/phase_r3/roc_sweep.json` | No |
| `results/phase_r3/selection_value.json` | No |
| `results/phase_r3/partial_window.json` | No |
| `results/phase_r3/event_charts/*.html` | No |
| `results/phase_r3/event_charts/index.html` | No |

---

## Reporting

Post: ROC sweep table, selection-value table, partial-window table, escalation check.
No winner recommendation. Cooper selects threshold (or disables ROC).

---

## Approval Gate

Do not begin R5 until Cooper has selected `roc_min` and given explicit approval.

---
---

## Phase R4 — LULD Halt-Avoidance Tuning

**DROPPED 2026-06-20.** Dependent on LULD exit module which is not used in EPG-Rapid. No replacement phase.

---
---

## Phase R5 — Integration & Confirmation (Milestone)

**Date:** TBD
**Baseline:** R0 baseline on full val
**Objective:** Run the fully selected config on full val; if it passes, open the test
split exactly once
**Primary success metric:** Full-val PF and CVaR5 within tolerance; CVaR5 ≥ −15%

---

**Context:**
- Config fully locked from R1–R4 selections. No parameter changes in R5.
- **Full val (1,228 events) used here for the first time.** The 100-event samples
  historically overstate PF by ~0.38 vs full val — account for this.
- **Test split opened exactly once, at the end of R5 T4, conditional on full-val pass.**
  If full val fails, test is not opened.

---

## Tasks

- [ ] **T1 — Full-val baseline**
  R0 baseline config (classic entry, EPG-Rapid exit stack) on full val. Gate-consistent
  comparator.

- [ ] **T2 — Full-val EPG-Rapid**
  Locked EPG-Rapid config (R1+R2+R3+R4 selections) on full val.

- [ ] **T3 — Confirmation analysis**
  Full-val EPG-Rapid vs (a) its own 100-event results and (b) full-val baseline.
  Metrics: PF, CVaR5, n_trades, win%, mean_entry_lag, exit-reason distribution.

- [ ] **T4 — Test split (conditional, final)**
  **Only if T3 passes all escalation criteria:** run the locked config once on the test
  split. This is the single, terminal test-set touch for the pipeline.
  - [ ] T4a — If any T3 criterion fails, do NOT run T4. Post results, await instruction.

- [ ] **T5 — Per-event charts**
  Standard 4-panel for all full-val traded events and (if T4 runs) all test events.
  Panel 1: 10s candlesticks, entry/exit markers. Panel 3: Setup filter — `q_tilde` trajectory, 0.65 threshold line, entry-eligible bars shaded green. Sortable index per sample.

---

## Escalation Criteria

| Condition | Threshold | Action |
|---|---|---|
| Full-val CVaR5 | < −15% | Hard stop — do not open test |
| Full-val EPG-Rapid PF | < full-val baseline PF | Hard stop — does not hold on full val |

---

## Output Files

| File | Commit? |
|---|---|
| `results/phase_r5/summary.md` | Yes |
| `results/phase_r5/fullval_baseline.json` | Yes (`-f`) |
| `results/phase_r5/fullval_rapid.json` | Yes (`-f`) |
| `results/phase_r5/confirmation.json` | Yes (`-f`) |
| `results/phase_r5/test_rapid.json` | Yes (`-f`) (conditional) |
| `results/phase_r5/event_charts/*.html` | No |
| `results/phase_r5/event_charts/index.html` | No |

---

## Reporting

Post: confirmation table (metric × {100-event rapid, full-val rapid, full-val baseline}),
halt-exposure table, test-split table (if opened), escalation check.

---

## Approval Gate

Do not proceed to paper trading integration until Cooper has reviewed R5 (including test)
and given explicit approval. Test split is now spent.

---
---

## Cross-Phase Notes

- **Selection authority:** every swept parameter selected by Cooper from presented data.
  Agents may not select winners.
- **C-phases have no backtest runs.** If an agent attempts a backtest during C1–C4, stop.
- **Full val:** R5 only. C-phases and R0–R4 are 100-event val (R4 adds halt-rich subsample).
- **Test split:** one touch, terminal, R5 T4, conditional on full-val pass.
- **Charts:** Panel 3 = Setup filter `q_tilde` trajectory, 0.65 threshold line, entry-eligible bars shaded green, throughout all R phases.
- **`results/phase_*/summary.md`** is a required output at every phase completion.
