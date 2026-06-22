<!-- fullWidth: false tocVisible: true tableWrap: true -->
---
tags:
  - type/plan
  - domain/backtest
  - project/scanner-epg-momentum
  - status/active
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

- **Isolation before integration.** Component validation phases (C2–C4) must complete
  before the first backtest runs. Each new code piece is unit-tested in isolation and
  approved before being incorporated into the next.
- **No cross-contamination.** A C-phase failure stops the sequence. Do not proceed to
  integration (R0) until all C-phases (C2–C4) are green.
- **Default sample (R0–R4):** MDR≥200 diagnostic sample — 100 events randomly selected
  from `mom_pct ≥ 200` AND `t_scanner_hit_sec IS NOT NULL`. Not stratified. Every event
  has a confirmed scanner hit. **Production val sample** (stratified, seed=42,
  scanner-confirmed, 1228 events) is used only in R5. Test split is never touched in C or
  R phases — opened once at the very end of R5.
- **No winner selection by agents.** Agents present data. Cooper selects all swept
  parameters. Algorithmic criteria only permit autonomous selection where explicitly stated.

---

## Phase Map

| Phase | Type | Purpose | Sample | Full val? |
|---|---|---|---|---|
| C0 | Component | Scanner hit floor — pre-computed catalog + floor guard. **Complete.** | Unit tests + single-event verify | No |
| C2 | Component | `LuldProximityExit` independent upper/lower + lower-enable flag | Unit tests | No |
| C3 | Component | Halt-gap clock pause in Hawkes/gate replay | Unit tests | No |
| C4 | Component | ROC 5-min buffer (per-ticker, partial-window, first-appearance) | Unit tests | No |
| R0 | Integration | Rapid runner + MDR≥200 diagnostic baseline; entry lag distribution | MDR≥200 (100 events) | No |
| R1 | Tuning | EPG gate threshold (`p_open` × `p_close`) — regime gate recalibration | MDR≥200 (100 events) | No |
| R3 | Tuning | 5-min ROC gate threshold | MDR≥200 (100 events) | No |
| R4 | Tuning | LULD halt-avoidance (two-sided, independent thresholds) | MDR≥200 (100 events) | No |
| R5 | Milestone | Integration + confirmation on production val sample | Production val (stratified, seed=42, scanner-confirmed) | **Yes** |

Phases are strictly sequential. Each requires Cooper's explicit approval before the next begins.

---
---

## Phase C0 — Scanner Hit Floor Fix (Correctness)

Status: COMPLETE (2026-06-22)

A warmup clock audit (T1–T5) found NO warmup clock bug, but uncovered a correctness
failure: the rapid runner processes all session trades from 4am to warm the Hawkes model.
The Hawkes anchor fires on the RTH open intensity surge — not on price momentum — so in
65.4% of val-split events the anchor (and therefore the gate warmup) fires BEFORE the
stock has reached the 30% threshold that triggers the live scanner. In live trading, we
would never see this name until it is a scanner hit.

XBP 2023-12-04 concrete case: anchor fired 9:30:07 ET, warmup expired 9:35:07 ET, entry
at 9:35:13 ET when stock was DOWN 4.4%. Scanner threshold not reached until 10:11:59 ET
(37 min later). `entry_lag_from_scanner_hit = −2,207s`.

Fix steps (A1–A5):

- **A1** — Pre-computed scanner hit catalog: `data/filtered/scanner_hit_catalog.json`.
  First trade where `price ≥ prev_close × 1.30` for each val-split event. Uses same
  `get_prev_close()` 3-source chain as the runner.
- **A2** — Floor guard added to `runner_rapid.py` as the first check in the entry loop:
  `if td.t_sec[i] < scanner_hit_t_sec: continue`. Events with no scanner hit in catalog
  are skipped entirely (`reason: no_scanner_hit`).
- **A3** — XBP 2023-12-04 single-event verify: entry now fires at 10:21:55 ET
  (595.6s after scanner hit vs −2,207s before). Gate was FAIL at scanner hit; entry
  occurred at next PASS tick. No escalation.
- **A4** — Full 100-event val run post-fix. See `results/scanner_floor_fix/post_fix_summary.json`.
  Awaiting Cooper review before R1 re-run.
- **A5** — Prior results invalidated: `_invalidated: true` injected into
  `phase_r0/rapid_r0/run_summary.json`, `phase_r0/baseline_r0/run_summary.json`,
  `phase_r1/symmetric_sweep.json`, `phase_r1/asymmetric_sweep.json`. `INVALIDATED.md`
  written at `results/phase_r0/` and `results/phase_r1/` directory roots.

Diagnostic fields added:

- Per-trade: `entry_lag_from_scanner_sec`
- Event-level: `gate_at_scanner_hit`
- Run summary: `mean_entry_lag_from_scanner_sec`, `median_entry_lag_from_scanner_sec`,
  `p90_entry_lag_from_scanner_sec`, `pct_events_gate_pass_at_scanner_hit`

**Output files:**

| File | Description |
|---|---|
| `data/filtered/scanner_hit_catalog.json` | Pre-computed scanner hit timestamps (val split) |
| `results/warmup_audit/audit_findings.md` | T1–T5 warmup clock audit; collateral finding |
| `results/warmup_audit/t4_event_table.json` | XBP 2023-12-04 timing table |
| `results/scanner_floor_fix/xbp_verification.json` | A3 single-event verify |
| `results/scanner_floor_fix/post_fix_summary.json` | A4 post-fix 100-event run |
| `results/scanner_floor_fix/fix_summary.md` | Fix narrative |

**Approval gate:** Do not re-run R0 or R1 until Cooper has reviewed A4 post-fix summary
and given explicit approval.

---
---

## Phase C2 — LuldProximityExit Independent Thresholds

**Objective:** Expose independent `proximity_threshold_upper` and `proximity_threshold_lower`
parameters on the rebuilt `LuldProximityExit` module, replacing the single shared threshold.
Add `lower_enabled: bool` config flag. Prove both sides can be configured independently
without regressing the existing test suite.

---

**Context:**

- No backtest run. Unit tests only.
- File: `backtest/core/exits/luld_proximity.py` — the **rebuilt** quote-based,
  sticky-reference module (Phase LULD-REBUILD).
- The rebuild currently uses a single `proximity_threshold` applied symmetrically. EPG-Rapid
  needs independent tuning (R4): upper and lower bands carry different false-exit profiles.
- `lower_enabled=False` must replicate prior behavior exactly (unit test T2a).
- All existing tests must continue to pass after the signature change.

---

## Tasks

- [ ] **T1 — Expose independent thresholds**
  Replace single `proximity_threshold` with `proximity_threshold_upper` and
  `proximity_threshold_lower`. Both default to the existing `proximity_threshold` value.
  Add `lower_enabled: bool = True` flag. Keep existing callers working via defaults.

- [ ] **T2 — Unit tests**
  - [ ] T2a — `lower_enabled=False`: module fires only on upper band; identical to prior
    locked config.
  - [ ] T2b — Both bands active, independent thresholds accepted.
  - [ ] T2c — Upper threshold only triggers upper exit (tick approaching upper band fires,
    lower does not).
  - [ ] T2d — Lower threshold only triggers lower exit (tick approaching lower band fires,
    upper does not).
  - [ ] T2e — All existing `test_luld_proximity.py` tests pass unchanged.

---

## Escalation Criteria

| Condition | Threshold | Action |
|---|---|---|
| Any existing test fails | any | Hard stop — post failures |
| `lower_enabled=False` differs from prior behavior | any | Hard stop |

---

## Output Files

| File | Description |
|---|---|
| `backtest/core/exits/luld_proximity.py` | Updated with independent thresholds |
| `backtest/tests/test_luld_proximity.py` | Updated/extended with T2 tests |

---

## Reporting

Post: test results, escalation check table.

---

## Approval Gate

Do not begin C3 until Cooper has reviewed and given explicit approval.

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

> **Invalidated (2026-06-22):** Prior R0 results (`rapid_r0/`, `baseline_r0/`) were
> generated before the scanner hit floor fix (C0). All metrics were produced with entries
> firing before the 30% scanner threshold. See `results/phase_r0/INVALIDATED.md`. Post-fix
> baseline is in `results/scanner_floor_fix/`. Awaiting Cooper approval of A4 post-fix
> summary before R0 re-run.

**Date:** TBD
**Baseline:** Classic EPG on 100-event val with EPG-Rapid exit stack — PF TBD at run time
**Objective:** Assemble all C-phase components into `runner_rapid.py`, prove parity against
the classic runner, and establish the gate-consistent baseline
**Primary success metric:** Parity diff empty; baseline metrics written

---

**Context:**

- All C-phases approved (C2–C4). All component unit tests green.
- **Sample: MDR≥200 diagnostic sample** — 100 events randomly selected from the event
  catalog where `mom_pct ≥ 200` AND `t_scanner_hit_sec IS NOT NULL`. Not stratified; not
  top-ranked. Saved to `data/val_mdr200_diagnostic.json`. Every event has a confirmed
  scanner hit — no `no_scanner_hit` skips.
- Scanner hit floor active (`scanner_floor=true`). `max_entry_lag_sec=null` in R0 (log only
  — Cooper sets the value after T7).
- ROC gate disabled (`roc_min=None`). Gate threshold: 0.65/0.65 (starting point).
- LULD: rebuilt module, both sides, starting thresholds (`proximity_threshold=0.02` each).
  EXIT_D disabled.
- Halt windows: `detect_luld_halts()` called per event; passed to replay.
- The classic runner path (`runner.py`) must not be modified.

---

## Tasks

- [ ] **T1 — Verify C-phase components integrated**
  Confirm all C-phase deliverables (C3: halt-gap pause; C4: RocBuffer) are importable and unit tests still pass in the
  assembled repo. Run full unit test suite. Any failure = stop before building the runner.

- [ ] **T2 — Build `runner_rapid.py`**
  Mirrors `runner.py` structure. Entry path diverges at the point where classic EPG waits
  for FAIL→PASS rising edge:
  - Rapid path: enters on the first live tick where `gate.state == GateState.PASS` (no rising-edge requirement, no `entry_eligible()`, no `n_hold`)
  - Feeds `halt_windows` to `_hawkes_replay_with_refit`
  - Sets `closed_today=True` at entry before fill, hard re-entry off
  - [ ] T2a — No `entry_eligible()` call anywhere in the rapid entry path. Confirm by code inspection and post the relevant entry logic block.

- [ ] **T3 — Parity check**
  Configure `runner_rapid.py` to mimic classic EPG exactly: `entry_mode=classic`
  (rising-edge), `roc_min=None`, EXIT_D config matching the classic baseline, and
  **`lower_enabled=False`** (classic `runner.py`'s locked config — per `backtest/CLAUDE.md`
  — is upper-only). Run on 100-event val. Produce trade-level JSON diff vs `runner.py` on same events.
  - [ ] T3a — Diff is empty. Any non-empty diff = hard stop.

- [ ] **T4 — Baseline runner (classic first-PASS on MDR≥200)**
  Classic first-PASS entry (first `gate.state == PASS` tick at or after `t_scanner_hit`),
  EPG-Rapid exit stack (LULD both sides on at starting thresholds, EXIT_D off, EPG close
  on). MDR≥200 sample. Write `results/phase_r0/baseline_metrics.json`. PF result IS the
  MDR≥200 baseline — the comparator for R1–R4.
  - [ ] T4a — Post PF, n_trades, CVaR5, exit-reason breakdown.

- [ ] **T5 — Rapid runner first-PASS on MDR≥200**
  EPG-Rapid first-PASS entry, same exit stack. Write `results/phase_r0/rapid_metrics.json`.
  Compare vs T4 baseline (entry method is the primary variable).

- [ ] **T6 — Pre-scanner audit (must be zero)**
  Query all trade records in T4 and T5 output for `entry_lag_from_scanner_sec < 0`. Any
  negative lag means the scanner floor is broken. Hard stop if any found. Post count.

- [ ] **T7 — Entry lag distribution**
  Compute `t_entry_sec − t_scanner_hit_sec` for all T5 trades. Report: mean, median, p10,
  p25, p50, p75, p90, p95. Histogram (log-scale x-axis). Flag: fraction of entries within
  60s of scanner hit (near-instantaneous); fraction > 1800s (30 min). This distribution is
  Cooper's input for setting `max_entry_lag_sec` before R1.

- [ ] **T8 — Per-event charts**
  Standard 4-panel per traded event (T5 output).
  - Panel 1: 10s candlesticks, entry/exit markers
  - Panel 2: I(t) = λ_sell / (λ_buy + λ_sell), theta line. No EXIT_D markers.
  - Panel 3: Setup filter — `q_tilde` trajectory, 0.65 threshold line, entry-eligible bars shaded green. Step-function, one value per 1-min bar. Scale 0–1.
  - Panel 4: EPG gate state (PASS / FAIL / WARMUP) as colored bands
  - [ ] T8a — Charts for all traded events
  - [ ] T8b — `results/phase_r0/event_charts/index.html` — sortable by ticker, date,
    session_bucket, n_trades, event_pf, exit_reason, mean_hold_sec

---

## Escalation Criteria

| Condition | Threshold | Action |
|---|---|---|
| Any C-phase unit test fails post-integration (T1) | any failure | Hard stop — post which test |
| Parity diff non-empty (T3a) | any diff | Hard stop — post the diff |
| T6 pre-scanner audit | any `entry_lag_from_scanner_sec < 0` | Hard stop — scanner floor is broken |
| Baseline PF (T4) | < 1.00 | Hard stop — MDR≥200 sample is strong movers; sub-1.00 indicates real signal problem |

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

> **Invalidated (2026-06-22):** Prior R1 results (symmetric sweep, asymmetric sweep,
> T3 charts) were generated before the scanner hit floor fix (C0). All sweep metrics
> are invalid. See `results/phase_r1/INVALIDATED.md`. Do not re-run R1 until Cooper
> approves the A4 post-fix summary and R0 is re-established as the post-fix baseline.

**Date:** TBD
**Baseline:** R0 baseline metrics
**Objective:** Recalibrate `p_open` and `p_close` for EPG-Rapid's role (primary exit
driver, no EXIT_D backing). Find the threshold that most cleanly detects regime state
without chattering.
**Primary success metric:** A presented sweep table with chatter and PF metrics across
all configs; Cooper selects

---

**Context:**

- **Sample: MDR≥200 diagnostic sample.** `max_entry_lag_sec` filter active (Cooper sets
  value after R0 T7 distribution; events where `t_entry − t_scanner_hit > max_entry_lag_sec`
  excluded from scoring). Starting point: 180s.
- `p_open` and `p_close` are not PF-optimization targets. The regime gate should correctly
  identify when momentum is active vs exhausted. PF is a downstream consequence.
- Entry: first-PASS (no SF, no `n_hold`). ROC disabled. LULD at starting thresholds (rebuilt
  module). These are held fixed to isolate the gate variable.
- Key diagnostics: **gate chatter** (mean PASS→FAIL transitions per trade — low = clean
  regime detection) and **exit-reason distribution**. Both reported per config.
- Asymmetric configs: `p_open > p_close` = enters on stronger signal, exits faster;
  `p_open < p_close` = enters more easily, holds through pullbacks.
- **Expected prior for MDR≥200 events:** low `p_open` / high `p_close` — enter early into
  strong momentum, hold through minor pullbacks. This is the asymmetric regime most natural
  for a strong mover that sustains for hours.

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

Do not begin R3 until Cooper has selected `p_open`/`p_close` and given explicit approval.

---
---

## Phase R3 — 5-Minute ROC Gate Tuning

**Date:** TBD
**Baseline:** First-PASS entry config (R1 gate threshold locked) with ROC disabled
**Objective:** Determine whether the 5-min ROC gate adds selection value and at what threshold
**Primary success metric:** Show whether any `roc_min` arm improves PF and/or CVaR5 vs
the disabled arm without unacceptable trade-count loss

---

**Context:**
- Gate threshold: R1 selection. Entry config: first-PASS (no SF, no n_hold). Locked.
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

**Date:** TBD
**Baseline:** R3 selection locked (ROC gate threshold)
**Objective:** Tune `proximity_threshold_upper` and `proximity_threshold_lower` independently
on the rebuilt module. Find the operating point that maximizes halt-avoidance precision while
minimizing false exits on strong movers.
**Primary success metric:** Frontier table (precision/recall tradeoff per side); Cooper selects.

---

**Context:**

- **Sample: MDR≥200 diagnostic sample.** `max_entry_lag_sec` filter active (R1 selection).
- Entry and gate config fully locked from R1 + R3.
- LULD module: rebuilt, both sides, **independent** upper/lower thresholds (C2 deliverable).
  Starting point: `proximity_threshold_upper=proximity_threshold_lower=0.02`.
- RTH only. Pre-market positions are not protected by LULD (exchange-discretion halts).
- **Tuning objective is precision/recall, not PF.** Maximize fraction of real halts exited
  ahead of; minimize false exits not followed by a halt. Cooper selects operating point.
- Upper and lower sides swept independently — a single symmetric threshold is expected
  to be suboptimal.

---

## Tasks

- [ ] **T1 — Halt labeling**
  Run halt detection (`detect_luld_halts()`) on all MDR≥200 events. Report: n_events with
  ≥1 halt, total halt count, halt distribution by session bucket (pre-market / RTH).

- [ ] **T2 — Upper-band sweep**
  `proximity_threshold_upper ∈ {0.005, 0.010, 0.015, 0.020, 0.030, 0.040}`.
  Lower held at 0.02. Per threshold: n_fires, n_halt_TP, n_false_exit (fire not followed
  by halt), recall (TP / total_halts), precision (TP / fires), mean_liquidity_penalty.

- [ ] **T3 — Lower-band sweep**
  `proximity_threshold_lower ∈ {0.005, 0.010, 0.015, 0.020, 0.030, 0.040}`.
  Upper at R2-selected threshold. Same metrics.

- [ ] **T4 — Precision/recall frontier**
  Plot (precision, recall) frontier per side. Flag dominant operating points. Present
  frontier only — no winner recommendation.

- [ ] **T5 — Per-event charts**
  Standard 4-panel for all traded events under the R4 selected config (post Cooper
  selection). Panel 1 annotated with LULD fire markers (upper ✕ orange, lower ◇ orange).
  Sortable index.

- [ ] **T6 — Reference-chase regression check**
  Confirm the rebuilt module's sticky-reference fix holds under two-sided tuning:
  band does not chase price during momentum run-up. Report: max observed
  reference-update frequency per event.

---

## Escalation Criteria

| Condition | Threshold | Action |
|---|---|---|
| Recall = 0 at all upper thresholds | — | Hard stop — post FN diagnostic |
| Recall = 0 at all lower thresholds | — | Flag — lower band may offer no protection |
| Any test in T1 fails (halt detection) | any | Hard stop |

---

## Output Files

| File | Commit? |
|---|---|
| `results/phase_r4/summary.md` | Yes |
| `results/phase_r4/halt_labels.json` | No |
| `results/phase_r4/upper_sweep.json` | No |
| `results/phase_r4/lower_sweep.json` | No |
| `results/phase_r4/frontier.json` | No |
| `results/phase_r4/event_charts/*.html` | No |
| `results/phase_r4/event_charts/index.html` | No |

---

## Reporting

Post: halt label summary, upper-band table, lower-band table, precision/recall frontier,
reference-chase check, escalation check. No winner recommendation.

---

## Approval Gate

Do not begin R5 until Cooper has selected `proximity_threshold_upper` / `_lower` and given
explicit approval.

---
---

## Phase R5 — Integration & Confirmation (Milestone)

**Date:** TBD
**Baseline:** R0 baseline re-run on production val sample
**Objective:** Run the fully selected config on the production val sample; if it passes,
open the test split exactly once.
**Primary success metric:** Production-val PF and CVaR5 within tolerance; CVaR5 ≥ −15%

---

**Context:**
- Config fully locked from R1–R4 selections. No parameter changes in R5.
- **Production val sample (stratified, seed=42, scanner-confirmed, 1228 events) used here
  for the first time.** The MDR≥200 diagnostic sample (R0–R4) was biased toward strong
  movers — expect some PF regression on the broader production sample.
- **Test split opened exactly once, at the end of R5 T4, conditional on production-val pass.**
  If production val fails, test is not opened.

---

## Tasks

- [ ] **T1 — Production-val baseline**
  R0 baseline config (classic first-PASS entry, EPG-Rapid exit stack) on production val
  (1228 events). Gate-consistent comparator.

- [ ] **T2 — Production-val EPG-Rapid**
  Locked EPG-Rapid config (R1+R3+R4 selections) on production val.

- [ ] **T3 — Confirmation analysis**
  Production-val EPG-Rapid vs (a) its own MDR≥200 diagnostic results and (b) production-val
  baseline. Metrics: PF, CVaR5, n_trades, win%, mean_entry_lag, exit-reason distribution,
  halt-exposure table (RTH vs pre-market).

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
- **C-phases have no backtest runs.** If an agent attempts a backtest during C-phases (C2–C4), stop.
- **MDR≥200 diagnostic sample (R0–R4):** 100 events randomly selected from `mom_pct ≥ 200`
  AND `t_scanner_hit_sec IS NOT NULL`. Not stratified; biased toward strong movers. Every
  event has a confirmed scanner hit — no `no_scanner_hit` skips. Built in Part B; saved to
  `data/val_mdr200_diagnostic.json`.
- **Production val sample (R5):** stratified, seed=42, scanner-confirmed, 1228 events.
  First used in R5 T1.
- **Test split:** one touch, terminal, R5 T4, conditional on production-val pass.
- **Scanner hit floor:** active in all R-phase runs. All entries at or after
  `t_scanner_hit_sec`. `entry_lag_from_scanner_sec` always ≥ 0 by construction. Pre-fix
  R0/R1 results are invalidated — see `results/phase_r0/INVALIDATED.md` and
  `results/phase_r1/INVALIDATED.md`.
- **LULD:** rebuilt module (Phase LULD-REBUILD), both sides, active in R0–R5. Starting
  thresholds `proximity_threshold_upper=proximity_threshold_lower=0.02`. Tuned in R4.
- **Charts:** Panel 3 = Setup filter `q_tilde` trajectory, 0.65 threshold line,
  entry-eligible bars shaded green, throughout all R phases. R4 T5 adds LULD fire markers
  to Panel 1.
- **`results/phase_*/summary.md`** is a required output at every phase completion.
