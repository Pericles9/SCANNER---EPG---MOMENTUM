---
tags:
  - type/results
  - domain/backtest
  - domain/microstructure
  - project/scanner-epg-momentum
  - status/wip
created: 2026-06-12
last_reviewed: 2026-06-12
---

# Phase LULD-REBUILD Results — Quote-Based LULD Exit

## Motivation

The Phase F LULD module (`core/exits/luld_proximity.py`) computed `upper_band` from a
rolling mean of **trade prices**, which chased price upward during momentum moves. As price
ran toward the band, the rolling mean followed, lifting the band above price and preventing
the exit from firing — then firing on the reversion instead of the approach. The result:
fires on reversals, not on genuine halt-proximity signals.

**Root cause:** reference price tracked price too closely (zero stickiness).

**Fix:** Replace with a **sticky 1% reference price** approximating SIP publication cadence,
and switch the proximity signal from trade price to **NBBO bid**. The bid approaches the
upper band first during a momentum run; using it instead of trade price gives earlier,
cleaner signal.

## Implementation (T1–T3)

### T2 — Module rewrite (`core/exits/luld_proximity.py`)

New interface:

```python
LuldProximityExit(
    ref_window_sec=300.0,      # 5-min rolling window for reference mean
    proximity_threshold=0.02,  # fraction: fire when (upper_band - bid) / upper_band ≤ this
    warmup_sec=60.0,
)
```

New `ProximityResult` fields:

| Field | Description |
|-------|-------------|
| `state` | INACTIVE / SAFE / EXIT_HALT |
| `fire_side` | `"upper"` or `None` (lower band permanently removed) |
| `reference_price` | Current sticky published ref price (0.0 before warmup) |
| `upper_band` | `round(ref * (1 + band_pct), 2)` |
| `bid_proximity_pct` | `(upper_band - nbbo_bid) / upper_band`; NaN if fallback |
| `spread_used` | ask − bid at this tick; 0.0 if no valid quote |
| `band_pct` | Current Tier 2 band pct (0.10 normal, 0.20 open/close windows) |

**Sticky reference price algorithm:**
1. Warm-up period (60 s of buffer span): return INACTIVE, don't set ref.
2. Cold start (first post-warmup tick): set `_published_ref` unconditionally.
3. Subsequent ticks: update `_published_ref` only if rolling mean moved ≥ 1% from current.
4. During EXIT_HALT (Limit State proxy): freeze `_published_ref`; resume when state clears.
5. Outside RTH or during warmup: reset `_in_limit_state = False` to prevent stale freeze.

**Proximity signal:** quote-based by default; falls back to trade price when bid ≤ 0 or
ask ≤ bid (logs warning once per reset cycle, counted as `luld_fallback_count`).

**Lower band:** permanently disabled — no `lower_band` field, no lower-band fires.

**Tests:** 26 tests pass (17 in `test_luld_proximity.py`, 9 in `test_luld_lower_gate.py`).
Full suite: 297/297 pass (excluding pre-existing `test_runner_sf` failure, confirmed
pre-existing on clean branch).

### T3 — Runner wiring (`runner.py`, `epg_replay.py`)

**`runner.py` changes:**
- Removed `luld_n_spread_multiple` and `luld_proximity_pct_threshold` throughout.
- New canonical parameter: `luld_proximity_threshold` (fraction, e.g. `0.02`).
- Config key resolution order (from `luld` section):
  1. `proximity_threshold` — new canonical key (fraction)
  2. `proximity_pct_threshold` — legacy pct key (divided by 100)
  3. `n_spread_multiple` — deprecated Phase E key (incompatible; logs warning, defaults to 0.02)
- Added `--luld-proximity-threshold` CLI arg; deprecated `--luld-n-spread-multiple`.
- `LuldProximityExit` now instantiated with `proximity_threshold=luld_proximity_threshold`.

**`epg_replay.py` changes:**
- Fixed broken constructor call (`proximity_pct_threshold=` kwarg removed, replaced with
  `proximity_threshold=` with same pct-divide-by-100 fallback logic).
- Fixed `luld_timeline` entry: `"lower_band"` → `"upper_band"`.

**Config:** `config/phase_luld_rebuild.json` uses `"proximity_threshold": 0.02`.

**Smoke test (T3b):** 5-event val sample (seed=42, `phase_f.json`) → 73 trades, PF=1.8406,
T2c assertion passed (luld_lower count=0). Deprecation warning fires correctly for
`n_spread_multiple` key.

---

## T4 — Baseline Run (proximity_threshold=0.02)

**Config:** `config/phase_luld_rebuild.json` | **Sample:** 100-event val, seed=42

**Events:** 84 with trades · 15 skipped · 1 error (ATLN 2024-06-26: quotes.parquet missing)

| Metric | Phase F baseline | LULD-REBUILD T4 | Delta |
|--------|-----------------|-----------------|-------|
| Overall PF | 2.2976 | **2.2766** | −0.021 ✓ |
| n_trades | 476 | 482 | +6 |
| win% | 50.63% | 49.59% | −1.0% |
| mean_pnl% | — | 1.1323% | — |

### Exit Breakdown

| Exit reason | Phase F n (%) | Phase F PF | T4 n (%) | T4 PF | T4 mean_pnl% |
|-------------|--------------|------------|----------|-------|---------------|
| exit_d | 232 (48.7%) | 1.9564 | 245 (50.8%) | 2.0492 | 1.09% |
| epg_window_close | 183 (38.4%) | 1.1744 | 198 (41.1%) | 1.0839 | 0.07% |
| **luld_upper** | **61 (12.8%)** | **13.47** | **39 (8.1%)** | **146.80** | **6.76%** |

luld_upper win%: 87.2% (34/39). Fires reduced 61 → 39 (more selective). PF jumped 13× .

### Escalation Checks — T4

| Check | Criterion | Result | Status |
|-------|-----------|--------|--------|
| luld_upper fire count | 5 ≤ n ≤ 120 | 39 | ✅ PASS |
| Fallback rate (max any event) | ≤ 20% | 16.4% (LVTX 2024-01-25) | ✅ PASS |
| Overall PF | ≥ 1.80 | 2.2766 | ✅ PASS |
| luld_upper PF | ≥ 11.0 (target) | **146.80** | ✅ EXCEEDS TARGET |

**Saved:** `results/phase_luld_rebuild/t4_baseline_summary.json` and `t4_baseline_trade_log.json`.

---

## T5 — Threshold Sweep

**Thresholds tested:** `[0.005, 0.010, 0.015, 0.020, 0.030, 0.040]`
**Sample:** 100-event val, seed=42, `config/phase_luld_rebuild.json` + `--luld-proximity-threshold`

**Phase F baseline (same sample):** PF=2.2976 · n=476 · luld_upper n=61 PF=13.47 win%=77.0% mean_pnl=4.20%

| thresh | Overall PF | Δ vs F | n_trades | win% | luld_n | luld% | luld PF | luld win% | luld mean_pnl% |
|--------|-----------|--------|----------|------|--------|-------|---------|-----------|----------------|
| 0.005 | **2.3809** | **+0.083** | 482 | 50.0 | 30 | 6.2% | **367.71** | — | 8.91% |
| 0.010 | 2.3148 | +0.017 | 482 | 49.6 | 33 | 6.8% | 222.84 | — | 7.88% |
| 0.015 | 2.2864 | −0.011 | 482 | 49.4 | 36 | 7.5% | 252.64 | — | 7.11% |
| 0.020 | 2.2766 | −0.021 | 482 | 49.6 | 39 | 8.1% | 146.80 | 87.2% | 6.76% |
| 0.030 | 2.2513 | −0.046 | 479 | 49.9 | 50 | 10.4% | 270.29 | — | 5.33% |
| 0.040 | 2.1856 | −0.112 ⚠️ | 467 | 49.2 | 61 | 13.1% | 60.65 | 73.8% | 3.95% |

(luld win% available only for 0.020 and 0.040 where trade_log.json was preserved.)

**Observations:**
- Overall PF **monotonically decreases** as threshold rises: tighter proximity → fewer fires → cleaner signal → higher overall PF.
- luld_upper PF is **non-monotonic**: the incremental trades entering the gate at each threshold step vary in quality. The 0.030 cohort is surprisingly strong (PF=270) while 0.040 marginal trades drag it to 60.
- thresh=0.040 is the **only value that breaches the −0.10 regression floor** (−0.112 ⚠️).
- thresh=0.005 fires on only 30 trades (n too low for confidence; may be driven by 1–2 events).
- thresh=0.010 is the **crossover**: still positive delta vs Phase F (+0.017), luld_n=33 (reasonable), luld PF=222.

**Saved:** `results/phase_luld_rebuild/t5_thresh{N}_summary.json` for all 6 values.

---

## T6 — Per-Event Charts (INCOMPLETE — AWAITING WINNER SELECTION)

**Status:** BLOCKED. T6 requires Cooper to select a winner threshold from the T5 sweep table.

### How to resume T6

1. **Cooper selects winner** from the T5 table above (e.g. "use 0.010").

2. **Re-run the winner** on the full 100-event val sample to get the trade_log.json with
   full bid_proximity_pct data (T5 only saved summary.json for most thresholds):
   ```bash
   python -m backtest.runner --split val --random-sample 100 --seed 42 \
     --config backtest/config/phase_luld_rebuild.json \
     --luld-proximity-threshold <WINNER>
   cp backtest/results/backtest/trade_log.json \
      backtest/results/phase_luld_rebuild/t6_winner_trade_log.json
   ```

3. **Generate per-event Plotly HTML charts** (one chart per event with trades):
   - X-axis: trade timestamp (ns → ET datetime)
   - Primary series: trade price
   - Overlay: `upper_band` and `reference_price` from per-trade luld state
   - Markers: entry, exits coloured by reason (luld_upper=red, exit_d=orange, epg_window_close=grey)
   - Subplot (optional): `bid_proximity_pct` time series with threshold line

4. **Save charts** to `results/phase_luld_rebuild/t6_charts/event_{ticker}_{date}.html`.

5. **Update CLAUDE.md** phase table: mark Phase LULD-REBUILD complete, record winner
   threshold and val-sample PF, and note delta vs Phase F baseline.

### Candidate thresholds for Cooper's consideration

| Candidate | Argument for | Argument against |
|-----------|-------------|-----------------|
| **0.010** | Positive delta vs Phase F (+0.017); luld_n=33; PF=222 | Only 33 fires may be insufficient for production stability |
| **0.015** | Balanced; small negative delta (−0.011); PF=252 highest in mid-range | Marginal improvement over 0.020 |
| **0.020** | T4 baseline; well-studied; luld_win%=87.2% confirmed | Negative delta vs baseline (−0.021) |
| **0.005** | Best overall PF (+0.083); luld PF=368 | Only 30 fires; high variance; possible sample concentration |
