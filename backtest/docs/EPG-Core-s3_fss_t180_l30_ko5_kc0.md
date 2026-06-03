---
tags:
  - type/implementation
  - domain/signal
  - domain/hawkes
  - project/scanner-epg-momentum
  - status/needs-review
created: 2026-05-29
last_reviewed: 2026-05-29
linked_code: "[[gate_variants.py]]"
config: "config/live_candidates/epg_core_s3_fss_t180_l30_ko5_kc0.json"
---

# EPG Core — s3_fss_t180_l30_ko5_kc0

Self-contained specification of the EPG (Event Participation Gate) core for config
`s3_fss_t180_l30_ko5_kc0`, isolated for pull-in to the live system. This is the
**total-PnL leader** of the EPG-OPT2-SF top decile (val noSF total_pnl = 3715, with SF = 2822).

## Purpose

Produce a live PASS/FAIL gate signal identical to the backtest for this config. The gate uses
the **SlopeGate** variant (F_ss: slope-open / slope-close) rather than the ParticipationGate the
live system currently runs. This doc specifies exactly what must be ported and how it wires into
the existing live Hawkes/anchor stack.

## Config Summary

| Param | Value | Meaning |
|-------|-------|---------|
| gate type | `SlopeGate`, mode `ss` | slope-open / slope-close (dead band) |
| `tau_sec` | 180.0 | half-life of the λ_V dollar-volume EMA |
| `L_sec` | 30.0 | slope lookback distance (seconds) |
| `k_open` | 0.5 | FAIL→PASS when `norm_slope ≥ 0.5` |
| `k_close` | 0.0 | PASS→FAIL when `norm_slope < 0.0` |
| dead band | `[0.0, 0.5)` | hold current state |
| `warmup_seconds` | 300.0 | WARMUP after T_event |
| `k_multiplier` (anchor) | 5 | T_event fires at `lambda_hat > 5·lambda_ref` |
| `use_setup_filter` | **false** | SF reduces total_pnl — keep OFF (see §Setup Filter) |

## Data Flow (per ticker, per session)

```
trades (price, size, ts) ─┬─> Hawkes online refit ──> lam_buy(t), lam_sell(t)
                          │        │
                          │        └─> cold_start MLE: mu_buy, mu_sell   (fit once, early)
                          │
                          ├─> EventAnchor(lambda_ref = mu_buy+mu_sell, k=5)
                          │        lambda_hat(t)=lam_buy+lam_sell; T_event = first crossing
                          │
                          └─> SlopeGate.update(dollar_vol = price*size, ts)
                                   activated at T_event
                                   λ_V EMA → norm_slope → PASS/FAIL
```

The two cold-start quantities `mu_buy` and `mu_sell` (from the initial MLE fit) feed **both**
the anchor's `lambda_ref` **and** the gate's `lambda_v_ref`. They must be the same values.

## Exact Computation (fidelity-critical)

1. **λ_V dollar-volume EMA** (per trade tick, `dt` = seconds since previous tick):
   ```
   λ_V(t) = λ_V(t-1) · exp(-ln2 · dt / 180) + (price·size) · (ln2 / 180)
   ```
   λ_V starts at 0.0 at `activate(T_event)`.

2. **Normalised slope** with `L_sec = 30`:
   ```
   norm_slope(t) = (λ_V(t) − λ_V(t − 30)) / (30 · lambda_v_ref)
   ```
   `lambda_v_ref = cold_start mu_buy + mu_sell` (clamped to ≥ 1e-9).

3. **Lookback buffer** — a deque of `(ts, λ_V)`. On each tick: append current, then prune the
   front **while `buf[1].ts ≤ (t − 30)`**. `λ_V(t−30)` = `buf[0].λ_V` once `buf[0].ts ≤ cutoff`;
   until then the slope is undefined and the gate returns **FAIL**.
   *(This prune rule — keep the most recent pre-cutoff entry, not "drop all older than cutoff" —
   is required for irregular tick spacing. Getting it wrong makes the gate never open.)*

4. **State machine (mode ss)** after WARMUP:
   - in FAIL: → PASS iff `norm_slope ≥ 0.5`
   - in PASS: → FAIL iff `norm_slope < 0.0`
   - otherwise hold (dead band)

5. **WARMUP**: for the first 300 s after T_event, `update()` returns `WARMUP` (no entries).

## Live Interface

**Inputs per update:** `dollar_vol = trade_price · trade_size`, `timestamp` (seconds, consistent
domain with T_event). Side is unused by this gate.

**State to carry:** `λ_V`, `last_timestamp`, the `(ts, λ_V)` deque, `in_pass`, `t_event`.

**Output:** `GateState ∈ {INACTIVE, WARMUP, PASS, FAIL}`. Entry on FAIL→PASS rising edge
(plus any existing live entry conditions). Exit on EPG window close (PASS→non-PASS) per the
current exit stack; EXIT_D / LULD layer on top unchanged.

**Lifecycle:** `activate(T_event)` once when the anchor fires; `update(dv, ts)` per trade;
`reset()` for a new session/continuation.

## Dependencies

| Component | Live source | Notes |
|-----------|-------------|-------|
| `SlopeGate` | `core/epg/gate_variants.py` | **New class to port** — live currently runs `ParticipationGate` only |
| `EventAnchor` | `core/epg/anchor.py` | already in live stack |
| Hawkes online refit + cold-start MLE | `core/hawkes/` | already in live stack; supplies `lam_buy/lam_sell` and `mu_buy/mu_sell` |
| `GateState` enum | `core/epg/gate.py` | shared |

Only `SlopeGate` is new. Everything else (anchor, Hawkes, cold-start mu) the live system already
computes for the current ParticipationGate path; this core re-uses those same quantities.

## Fidelity Notes (do not skip)

- **`lambda_v_ref` is an arrival rate, not a dollar-volume rate.** The normaliser is
  `mu_buy + mu_sell` (events/sec) while λ_V is in $·(ln2/τ) units. This dimensional mismatch is
  *intentional to preserve* — `k_open=0.5` / `k_close=0.0` were calibrated against exactly this
  normalisation. Live must use the same cold-start `mu_buy+mu_sell`, not a $-volume reference.
- **Same mu for anchor and gate.** Use one cold-start fit; feed its `mu_buy+mu_sell` to both
  `EventAnchor.lambda_ref` and `SlopeGate.lambda_v_ref`.
- **Buffer prune rule** (see §3) is the single most error-prone port. Unit test it against
  irregular tick spacing where a gap straddles the `t − L_sec` cutoff.
- **High trade frequency.** This config produced ~19k–25k entries on 100 val events
  (mean hold ~32–39 s). It re-opens frequently — expect dense order flow. Confirm live
  throttling / order-rate limits tolerate this before deploying.

## Setup Filter

`use_setup_filter = false`. EPG-OPT2-SF showed the setup filter cuts total_pnl on every
top-decile config. For this config specifically, SF blocked 23.75% of entries and removed
~894 total_pnl_pct on val (3715 → 2822). Keep the setup filter OFF as an entry gate.
(The continuous-disqualifier role of the SF for universe removal is a separate live concern,
out of scope here.)

## Validation Metrics (val seed=99, 100 events)

| | no-SF | with-SF |
|--|-------|---------|
| total_pnl_pct | 3715.3 | 2821.6 |
| profit_factor | 1.338 | 1.340 |
| capture_rate | 0.00389 | 0.004671 |
| capture_fraction | −4.82 | −4.58 |
| n_trades | 24,739 | 18,863 |
| win_rate | 46.4% | 47.1% |
| mean_hold_sec | 38.6 | 32.0 |

## Related

- [[results.md]] — Phase EPG-OPT2-SF results (results/phase_epg_opt2_sf/)
- [[gate_variants.py]] — SlopeGate implementation
- [[gate.py]] — ParticipationGate + GateState (current live gate)
- Config: `config/live_candidates/epg_core_s3_fss_t180_l30_ko5_kc0.json`
