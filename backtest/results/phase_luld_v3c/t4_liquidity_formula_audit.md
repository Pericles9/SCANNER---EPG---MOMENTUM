# Phase LULD-V3c — T4: Liquidity-Penalty Formula Audit

**Date:** 2026-06-20
**Verdict:** **CONFIRMED DEFECT (units).** The liquidity penalty is emitted as **raw
`spread_bps`** (basis points, unbounded) and then subtracted directly from `recall` (max 3.0)
and `fp_rate` (max 1.0) in the composite. With a mean of ~59 bps and a max of 526 bps, the
penalty term swamps the entire objective — the composite in V3b T6 was, to two significant
figures, just the negative liquidity penalty. The weights are **not** the problem; the
penalty *scale* is.

---

## Code reference

`core/exits/luld_scoring.py:147-159`:
```python
for f in fires:
    if f.mid_price > 0:
        shares_needed = position_value_usd / f.mid_price
        liq_penalty = f.spread_bps if f.bid_size_shares < shares_needed else 0.0
    else:
        liq_penalty = f.spread_bps
    liq_penalties.append(liq_penalty)
mean_liq = sum(liq_penalties) / len(liq_penalties) ...
composite = w_recall * recall - w_fp * fp_rate - w_liq * mean_liq
```

`liq_penalty` is assigned `f.spread_bps` directly — a basis-point figure, typically tens to
hundreds. It is never normalized. The module docstring (`luld_scoring.py:20-24`) describes
exactly this raw-bps behaviour, so the code is self-consistent with its own spec — but that
spec produces an out-of-scale term.

> Note on the V3c prompt's referenced formula. The V3c brief cites an intended formula
> `max(0, target_spread_bps − spread_bps) / target_spread_bps`. That formula does **not**
> appear in the code, and as written it is also *inverted* for use as a penalty (it →1 when
> the spread is tight and →0 when wide, i.e. it rewards illiquidity). The actual implemented
> term is raw `spread_bps`. The defect is the missing normalization, not a sign error.

---

## Evidence — 10-fire intermediate-value table (dur=0)

| ticker | spread_bps | bid_size | mid | shares_needed | bid<needed? | raw_penalty |
|--------|-----------:|---------:|------:|--------------:|:-----------:|------------:|
| CRBP | 50.28 | 100 | 19.8900 | 50.3 | no | 0.00 |
| CRBP | 78.66 | 100 | 20.3400 | 49.2 | no | 0.00 |
| CRBP | 4.94 | 100 | 20.2450 | 49.4 | no | 0.00 |
| XBP | 368.68 | 200 | 27.3950 | 36.5 | no | 0.00 |
| MNPR | 96.62 | 4000 | 1.0350 | 966.2 | no | 0.00 |
| MNPR | 97.56 | 100 | 1.0250 | 975.6 | **yes** | **97.56** |
| MNPR | 96.62 | 800 | 1.0350 | 966.2 | **yes** | **96.62** |
| IVP | 136.67 | 100 | 2.1950 | 455.6 | **yes** | **136.67** |
| IVP | 136.67 | 100 | 2.1950 | 455.6 | **yes** | **136.67** |
| IVP | 91.32 | 500 | 2.1900 | 456.6 | no | 0.00 |

**Raw-penalty range across all sampled fires: [0.00, 526.32], mean = 59.01.**

### Term-by-term check (per the T4 audit checklist)

- **Is `spread_bps` actually in bps?** Yes — `(ask − bid)/mid × 10_000`. The values
  (5–369 bps) are plausible *real* spreads for sub-$30, low-float momentum names near a LULD
  band. So the bps figures are correct; they are simply being used unscaled as a penalty.
- **Is `target_spread_bps` zero / blowing up a division?** N/A — there is **no** division and
  no `target_spread_bps` in the implemented formula. The penalty is the bps value itself.
- **Is the size-insufficiency branch firing unconditionally?** No. `shares_needed =
  $1000 / mid`. For $1–2 names (MNPR, IVP) `shares_needed` is 450–975 shares, frequently above
  the prevailing bid size (100–800), so the branch fires *legitimately*. For ~$20–27 names
  (CRBP, XBP) `shares_needed` is 36–50 shares, usually ≤ bid size, so the branch correctly
  does **not** fire. The branch logic is sound; only its output scale is wrong.

---

## Two findings, kept separate

1. **Units defect (fix in T5a).** Normalize the penalty to ~[0, 1] so the `w_liq = 1.0`
   weight is commensurate with `w_fp = 1.0` and the `recall` term. Proposed:
   `liq_penalty = min(1.0, spread_bps / TARGET_SPREAD_BPS)` when `bid_size < shares_needed`,
   else `0.0`, with `TARGET_SPREAD_BPS` a documented constant (the spread at which fill
   quality is treated as fully impaired). **Weights unchanged**, per spec.

2. **Genuine illiquidity is real — not an artifact.** Even after normalization, the penalty
   may remain high, because these fires occur *by design* when the bid is pinned within 1% of
   the LULD band — exactly when market makers thin out and spreads widen (50–370 bps observed).
   That is a true property of the exit, consistent with Phase F (LULD depresses PF). If the
   normalized mean still exceeds the 0.5 hard-stop after T5a, that is a **design signal for
   Cooper**, not evidence of a remaining bug. T5a reports the normalized number without
   tuning `TARGET_SPREAD_BPS` to dodge the threshold.
