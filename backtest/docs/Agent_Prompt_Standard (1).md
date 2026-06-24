# Agent Prompt Standard

**Version:** 1.1  
**Project:** Scanner × Hawkes × OFI — Momentum Trading System

This document defines the standard structure for all Claude Code agent prompts in this project. Every phase prompt must follow this format. Deviations require explicit justification in the prompt itself.

---

## Why This Exists

Agent prompts have grown organically across phases. Inconsistent structure causes two problems:

1. Agents make judgment calls they shouldn't (self-resolve vs. escalate)
2. Results are hard to audit because output contracts vary phase to phase

This standard fixes both.

---

## Prompt Structure

Every agent prompt has these eight sections, in this order.

---

### 1. Header Block

One paragraph. Covers: what phase this is, what the previous phase established, what this phase changes or validates, and the primary success metric.

```
## Phase [X] — [Short Name]

**Date:** YYYY-MM-DD  
**Baseline:** [Prior phase] — [val sample], PF=[X.XXXX], [N] trades  
**Objective:** [One sentence — what this phase accomplishes]  
**Primary success metric:** [e.g., PF > 1.53 on 100-event val sample, seed=42]
```

Keep it tight. If the agent needs more context, link to the relevant result file — don't inline it.

---

### 2. Context & Constraints

A short bullet list of facts the agent must hold in mind. These are not tasks — they're operating constraints.

```
**Context:**
- Val sample: 100 events, seed=42 (stratified). Full 1,228-event val is reserved for milestone runs only.
- Train/val/test separation is strictly enforced. No test set access.
- Hardware: Ryzen 5 3600, GTX 1070 (FP32 only), 32GB RAM. No CUDA FP16.
- Working directory: [repo root]
- Config files live in config/. Do not modify configs that belong to a prior phase without explicit instruction.
- [Any phase-specific constraints]
```

List only what's actually relevant to this phase. Don't copy-paste the full project context every time.

---

### 3. Task Checklist

A numbered checkbox list. Each item is a discrete, verifiable unit of work.

Rules:
- Tasks are sequential unless explicitly marked `[PARALLEL OK]`
- Each task produces a concrete artifact (file, metric, log line) — no open-ended tasks
- If a task requires a decision (e.g., parameter selection), state the selection criterion explicitly so the agent doesn't invent one
- Break compound tasks into sub-tasks with indented checkboxes

```
## Tasks

- [ ] **T1 — [Short label]**  
  [What to do. What file to write. What criterion to use if a choice is involved.]

  - [ ] T1a — [Sub-task if needed]
  - [ ] T1b — [Sub-task if needed]

- [ ] **T2 — [Short label]**  
  [Same pattern]
```

---

### 4. Escalation Criteria

An explicit table. The agent checks every row after each task that could trigger one.

```
## Escalation Criteria

Stop and post results. Do not proceed to the next task.

| Condition | Threshold | Action |
|-----------|-----------|--------|
| [Metric] [comparison] [value] | e.g., PF < 1.30 | Hard stop — post results, await instruction |
| [Metric] [comparison] [value] | e.g., null_spread_pct > 5% | Hard stop — post results, await instruction |
| [Condition] | e.g., Any WF window 5th pct < 1.0 | Hard stop — post results, await instruction |
```

**Hard stop** means: post results, explain which criterion was triggered and the observed value, and wait. The agent does not attempt to fix the problem or move to the next task.

If no escalation criteria apply to a task, state that explicitly: `No escalation criteria for this task.`

---

### 5. Output File Contract

A table listing every file this phase must produce. Agent marks status as it goes.

```
## Output Files

| File | Description | Status |
|------|-------------|--------|
| `results/phase_[x]/[task]/[filename].json` | [What it contains] | [ ] |
| `results/phase_[x]/[task]/charts/[name].html` | [Chart description] | [ ] |
| `config/[name].json` | [What params it holds] | [ ] |
```

Rules:
- Every output file must be listed before the agent starts
- If a file is conditional (e.g., only written on escalation), note that in the description
- The agent must not write files to locations not listed here without posting to chat first

---

### 6. Reporting Format

Tells the agent exactly what to post when the phase is complete (or when escalating).

```
## Reporting

On completion, post:
1. Comparison table: [prior baseline] vs. Phase [X] — columns: [list the metrics]
2. Exit breakdown table: count and % for each exit type
3. Escalation check table: each criterion, observed value, pass/fail
4. Walk-forward table if applicable
5. Output file table with final status column filled in
6. [Any phase-specific charts or summaries]

On escalation, post:
1. Which criterion triggered and the observed value
2. The metrics table up to the point of failure
3. No recommendations — present data only
```

---

### 7. Per-Event Charts

**This section is mandatory for every phase that produces trade records.** Analysis-only phases (no backtest run) are exempt but must state that explicitly.

Per-event charts are the primary tool for keeping the strategy auditable. They turn backtest output into something that can be read and inspected, not just measured.

#### Why this is required

- Aggregate metrics (PF, win rate) can mask event-level pathology — a handful of outlier events can carry or drag the whole sample
- Signal behavior on individual events reveals whether exits, entries, and gates are firing for the right reasons
- Without per-event charts, parameter changes are optimizing into a black box

#### Standard chart format

Every per-event chart is a **standalone Plotly HTML file** with a **4-panel layout**, shared x-axis, vertical shading for EPG PASS windows:

| Panel | Content | Always required |
|-------|---------|-----------------|
| 1 — Price | 10s candlesticks + entry markers (first entry: green ▲, re-entry: blue ▲) + exit markers (green ▼ = win, red ▼ = loss) + LULD fires (orange ✕) | Yes |
| 2 — Sell intensity I(t) | `I(t) = λ_sell / (λ_buy + λ_sell)`, theta threshold line, EXIT_D fire markers (orange ◆) | Yes |
| 3 — Buy intensity I_buy(t) | `I_buy(t) = λ_buy / (λ_buy + λ_sell)`, `(1 − theta)` threshold line, re-entry fire markers (blue ◆) | Yes if re-entry module is active; else omit panel or replace with phase-specific signal |
| 4 — EPG state | PASS / FAIL / WARMUP as colored horizontal bands | Yes |

Panel 3 content adapts per phase. If re-entry is not active, replace it with the most diagnostic signal introduced in that phase (e.g., LULD proximity ratio, OFI autocorr). Document the substitution in the prompt.

#### Coverage requirement

- Charts must be produced for **all events that generated at least one trade**
- Skipped and errored events are excluded
- No sampling — do not produce charts for a subset unless runtime makes full coverage impossible (log a note if so)

#### Index file

Every phase must also produce a **sortable HTML index** at `results/phase_{x}/event_charts/index.html`.

The index must be sortable by: ticker, date, session, n_trades, n_reentries (if applicable), event_pf.
Each row links to the individual event chart.

#### Output path convention

```
results/phase_{x}/event_charts/{TICKER}_{DATE}.html   ← one per event
results/phase_{x}/event_charts/index.html              ← sortable index
```

#### Chart task template

Add this task to every phase prompt that runs a backtest:

```
- [ ] **T[N] — Per-event charts**
  Produce one 4-panel Plotly HTML chart per traded event using the standard panel layout
  defined in Agent_Prompt_Standard.md §7. Write to `results/phase_{x}/event_charts/`.
  Adapt Panel 3 to [phase-specific signal or re-entry if active].

  - [ ] T[N]a — Charts written for all [N] events with trades
  - [ ] T[N]b — Sortable index written to `results/phase_{x}/event_charts/index.html`
```

---

### 8. Approval Gate

The final line of every prompt. No exceptions.

```
## Approval Gate

Do not begin Phase [X+1] or any follow-on work until Cooper has reviewed results and given explicit approval.
```

---

## Anti-Patterns

These are things that have caused problems in past phases. Don't do them.

| Anti-pattern | Why it's a problem |
|---|---|
| Agent resolves an escalation by tweaking a parameter | Produces untracked changes; bypasses validation discipline |
| Tasks that don't produce a verifiable artifact | No way to confirm the task was done correctly |
| Selection criterion not specified (e.g., "choose the best gamma") | Agent invents a criterion, which may not match project intent |
| Output file table omitted | Files end up in inconsistent locations across phases |
| Escalation criteria missing units or direction | Agent misinterprets (e.g., "PF fails" is ambiguous — is 1.52 a failure?) |
| Per-event charts omitted or sampled | No way to audit whether signal behavior is correct on individual events; aggregate metrics mask pathology |
| Panel 3 substitution undocumented | Agent picks an arbitrary signal; chart meaning is ambiguous across phases |
| Index file missing | Per-event charts exist but can't be navigated efficiently |
| Phase context duplicated in full from prior prompts | Inflates token usage; key constraints get buried |
| Multiple simultaneous hard-stop conditions with no priority | Agent doesn't know which to report first |

---

## Minimal Working Example

```markdown
## Phase J — Event-Level PnL Analysis

**Date:** 2026-04-12  
**Baseline:** Phase H H4 full val — 1,088 events, PF=1.5297, 160,710 trades  
**Objective:** Compute event-level PnL aggregates and identify the top/bottom decile drivers  
**Primary success metric:** Event-level summary file written with no missing events from H4

---

**Context:**
- Source: `results/phase_h/h4_full_val/event_level_summary.json` (1,088 events)
- Val sample: full 1,228-event split (this is a milestone analysis run)
- No parameter changes in this phase — analysis only
- Charts: Plotly interactive HTML, standalone files, one chart per file

---

## Tasks

- [ ] **T1 — Load and validate event-level summary**  
  Load `event_level_summary.json`. Confirm 1,088 events present. Log any events with null PnL.
  - [ ] T1a — If null PnL count > 10, escalate before proceeding

- [ ] **T2 — Compute decile breakdown**  
  Sort events by total_pnl_usd. Split into deciles. For each decile compute: mean PF, mean hold_sec, exit type distribution, mean S score.

- [ ] **T3 — Write charts**  
  - [ ] T3a — `01_event_pnl_ranked.html` — ranked bar chart of event PnL
  - [ ] T3b — `02_decile_feature_heatmap.html` — heatmap of features by decile

---

## Escalation Criteria

| Condition | Threshold | Action |
|-----------|-----------|--------|
| Null PnL event count | > 10 | Hard stop — post null count and example events, await instruction |
| Bottom decile mean PF | < 0.5 | Hard stop — post decile table, await instruction |

---

## Output Files

| File | Description | Status |
|------|-------------|--------|
| `results/phase_j/event_decile_summary.json` | Per-decile feature aggregates | [ ] |
| `results/phase_j/charts/01_event_pnl_ranked.html` | Ranked event PnL bar chart | [ ] |
| `results/phase_j/charts/02_decile_feature_heatmap.html` | Feature heatmap by decile | [ ] |

---

## Reporting

On completion, post:
1. Decile table: decile, n_events, mean_pnl_usd, mean_pf, mean_hold_sec, dominant_exit_type
2. Escalation check table
3. Output file table with status filled in

On escalation, post:
1. Which criterion triggered and observed value
2. Relevant raw data — no recommendations

---

## Approval Gate

Do not begin Phase K or any follow-on work until Cooper has reviewed results and given explicit approval.
```

---

## Version History

| Version | Date | Change |
|---------|------|--------|
| 1.1 | 2026-05-10 | Added §7 Per-Event Charts as mandatory standard deliverable for all backtest phases |
| 1.0 | 2026-04-11 | Initial standard |
