# LULD Halt Architecture
## Reference Document for EPG Exit Rebuild

---

## 1. What LULD Actually Is

LULD (Limit Up-Limit Down) is a **quote-based** mechanism. That distinction matters.

The old single-stock circuit breaker was trade-based — it only fired after bad trades had already happened. LULD is preventive: it works by preventing quotes from being displayed or executed outside specified price bands. By the time a halt fires, the quote has been pinned at the band for at least 15 seconds.

The sequence is:

```
Price runs → NBO touches Upper Band (or NBB touches Lower Band) → LIMIT STATE
  → 15-second clock starts
  → If resolved: normal trading resumes (most cases)
  → If NOT resolved: PRIMARY EXCHANGE DECLARES 5-MIN TRADING PAUSE
```

**The halt is never instant.** There is always a minimum 15-second warning window between the Limit State entry and the actual halt. This is the window the exit should exploit.

---

## 2. Tier Classification

The band width depends on which tier the stock falls into. **This is determined by the previous day's closing price and index membership — it does not change intraday.**

| Tier | Securities |
|------|-----------|
| **Tier 1** | S&P 500, Russell 1000, select high-volume ETPs |
| **Tier 2** | All other NMS stocks (our gap candidates live here) |

Tier 2 is the relevant tier for EPG gap stocks.

---

## 3. Band Width Schedule

Bands are calculated as: `Price Band = Reference Price ± (Reference Price × Band %)`, rounded to the nearest penny.

### Tier 2 (our universe)

| Price Level | Normal Hours (09:45–15:35 ET) | Open Window (09:30–09:45) + Close Window (15:35–16:00) |
|-------------|-------------------------------|--------------------------------------------------------|
| > $3.00 | **10%** | **20%** |
| $1.00–$3.00 | 20% | 40% |
| $0.75–$1.00 | 40% | 80% (or lesser of $0.15/75%) |
| < $0.75 | 75% or $0.15 (lesser of) | Doubled |

**Key facts for our gap stocks:**

Most EPG candidates are > $3.00, so the operative band is **10% normal, 20% doubled**. For the early pre-market window where EPG fires, LULD does not apply (pre-market is out of scope — LULD only covers 09:30–16:00 ET). For RTH entries, bands start at 20% from 09:30–09:45 and drop to 10% from 09:45 onward.

**Note:** The band percentage tier is locked at the previous close price, not the current price. A stock that opens at $5 after a 30% gap is still evaluated against the tier bucket based on its prior close (~$3.85), not the current $5 price.

---

## 4. Reference Price

**Definition:** Arithmetic mean of all eligible reported transactions over the prior 5-minute rolling window, as published by the SIP.

**Key behaviors:**

- **Updates with a 1% minimum change filter.** The SIP only publishes a new reference price if it is at least 1% away from the current reference price. Minor oscillations do not move the bands.
- **Frozen during a Limit State.** Once a stock enters a Limit State, no new reference price or bands are published until the Limit State resolves.
- **Ineligible transactions excluded.** VWAP prints, certain exempt trades, and transactions that don't update the last sale price are excluded from the reference calculation.
- **Opening reference price.** At the open (09:30–09:35), there is no 5-minute history yet. The opening auction price is used. If no auction occurs, the prior day's closing price is the fallback.
- **If no eligible trades in 5 min:** The previous reference price stays in effect.

**Implication for a 30%+ gap-up stock:** On RTH open, the reference price is set to the opening auction print — likely near the gap price. Bands are immediately calculated from that level. Because our stocks are moving fast, the reference price will be chasing price upward throughout the event.

---

## 5. The Limit State — Precise Definition

A stock enters a **Limit State** when:

- **Limit Up:** The National Best Bid (NBB) equals the Upper Price Band (without crossing it)
- **Limit Down:** The National Best Offer (NBO) equals the Lower Price Band (without crossing it)

The SIP flags the resting quote at the band as a "Limit State Quotation." The opposing side quote (if outside the band) is flagged as "Non-Executable."

**Exiting a Limit State (within 15 seconds):**
All Limit State Quotations must be executed or canceled in their entirety. If that happens, normal trading resumes — no halt. Most Limit States resolve this way.

**Failing to exit (halt trigger):**
If the Limit State persists for 15 continuous seconds, the Primary Listing Exchange declares a 5-minute Trading Pause. All executions stop across all venues.

**Straddle State (distinct from Limit State):**
When the NBB falls *below* the Lower Price Band (or NBO rises *above* the Upper Price Band), without the other side touching the band, a "Straddle State" exists. No 15-second clock, no automatic halt — but the primary exchange *may* declare a pause. Straddle States are a softer warning signal.

---

## 6. The Full Halt Sequence (Timeline)

```
T=0s    NBB touches Upper Band  →  LIMIT STATE entered
        SIP flags quotes, freezes reference price and bands
        15-second clock starts

T=0–15s  Market participants can execute or cancel Limit State Quotations
         Most Limit States end here with no halt

T=15s   Limit State still unresolved  →  PRIMARY EXCHANGE DECLARES TRADING PAUSE
        All executions halt across all venues
        Orders remain on book unless customer cancels
        New orders CAN be submitted during the pause

T=15s–5min  Quote-only period; NOII published every 5 seconds on Nasdaq
             No executions

T~5min  Primary exchange attempts reopening auction
        Auction collar = LULD Band ± 5% (direction of trigger side)
        Reopening can happen at any point after first extension if within collar

If no reopen by 3:50pm ET:  Volatility Closing Auction at 4:00pm instead
```

---

## 7. What This Means for the Exit Signal

### The original intent

Exit *before* the halt — ideally during the Limit State window or just before it's entered. The theoretical signal is **the NBB approaching the Upper Price Band**, not a price deviation from a rolling mean.

### What the current implementation does wrong

The current `luld_proximity.py` measures price deviation from a **trailing 5-minute rolling mean** (the reference price approximation) and fires an exit when price is within `n_spread_multiple × spread` of the computed band. This is band proximity detection — conceptually close, but it has two structural problems:

1. **The reference price drifts upward with momentum.** During a strong run, the 5-minute mean chases price up, which means the band edge also chases price up. The exit fires when price *pulls back toward* the band, which is exactly the wrong moment — it's a reversal signal masquerading as a halt-prevention signal.

2. **It doesn't observe the actual Limit State.** A real LULD exit should fire when the *quote* (NBB or NBO) touches the band — not when the *trade price* is close to a modeled band. These are different things. A quote can pin at the band while the last trade is still below it.

### What the correct exit observes

The actual observable that precedes a halt is: **the best bid (NBB) approaching or touching the Upper Price Band.** That is quote data, not trade data.

The sequence in observable data:
1. Ask starts collapsing toward the band edge — spread compresses dramatically
2. NBB lifts to match or near the Upper Band → Limit State entry
3. 15-second clock runs; if not resolved → halt

For an exit signal, the goal is to exit during step 1 or at the start of step 2, before the halt freezes execution.

---

## 8. Scope Constraints

| Condition | LULD Status |
|-----------|------------|
| Pre-market (before 09:30 ET) | **Out of scope — LULD does not apply** |
| 09:30–16:00 ET | LULD active |
| Post-market (after 16:00 ET) | Out of scope — exchange-discretion halts only |
| Rights, warrants | Excluded from LULD entirely |
| Securities not in NMS | Excluded |

**This matters for EPG:** The majority of our entries are pre-market. LULD is irrelevant for those. The module should be completely inactive before 09:30 ET. For the minority of RTH entries, LULD applies, and the band widths are doubled for the first 15 minutes (09:30–09:45).

---

## 9. Design Constraints for the Rebuilt Exit

### What we need from quote data

| Field | Why |
|-------|-----|
| `nbbo_bid` | The signal — touching Upper Band is the halt trigger |
| `nbbo_ask` | Spread compression is an early warning |
| Band upper value | Computed from SIP reference price + tier % |

### Reference price reconstruction

We cannot receive the actual SIP reference price from Polygon's trade feed. The reconstruction approximation (5-min arithmetic mean of trades) is correct in principle but has the 1% update filter behavior — the real SIP bands don't move on every tick, they update discretely. Our reconstruction is *more responsive* than the actual feed, not less. This creates more false-positive proximity signals when price oscillates near the band.

### The 1% update filter as a design feature

The real bands only move when the new reference price is ≥1% from the current one. This means **bands are sticky** — price can run several percent without the band moving at all. Our continuously-updated reconstruction doesn't have this stickiness, which inflates proximity signals.

For the rebuild, we should consider applying the 1% minimum-change filter to our reference price computation to better approximate actual SIP behavior.

### Upper-only, not lower

As already established in Phase E/F analysis: the lower band exit is harmful (pre-empts legitimate exits during pullbacks). Only the Upper Band exit is in scope.

### Pre-market is out of scope

The module must return `INACTIVE` for any timestamp before 09:30 ET. Pre-market gap halts are exchange-discretion events with no standardized formula and are not predictable via LULD band math.

---

## 10. Open Design Questions for the Rebuild

These are the decisions that need to be made before writing code:

1. **Quote-based vs trade-based signal.** The rebuilt exit should ideally use `nbbo_bid` approaching the Upper Band rather than trade price. Do we have reliable bid data at sufficient granularity in the Polygon quotes feed for pre-RTH and early-RTH? (If not, trade-price approximation may be unavoidable, but the drift issue needs a fix.)

2. **Apply the 1% SIP update filter.** Should we gate our reference price updates to only move when the new mean is ≥1% from current? This makes our simulation more realistic and reduces false proximity signals.

3. **Warmup period.** At RTH open, the opening auction print is the reference price. We should not fire the exit during the first few seconds before the reference is established.

4. **Band width schedule edge.** The open-window doubled band (20%) runs 09:30–09:45. A gap stock entering at 09:31 with a 20% upper band is very unlikely to halt upward. The exit is most relevant in the normal-hours window (10% band) for RTH re-entries.

5. **What constitutes "approaching" the band.** The current spread-multiple buffer is conceptually reasonable but was calibrated on the wrong signal. With a quote-based implementation, a simpler threshold (e.g., bid within X% of Upper Band, or bid within Y cents) may be more stable.

---

## 11. Summary: What to Build

| Component | Specification |
|-----------|--------------|
| **Active window** | 09:30–16:00 ET only; `INACTIVE` outside |
| **Tier** | Tier 2 for all EPG gap candidates (Tier 1 irrelevant) |
| **Band pct** | 20% from 09:30–09:45; 10% from 09:45–15:35; 20% from 15:35–16:00 |
| **Reference price** | 5-min arithmetic mean of eligible trades; update only if ≥1% change from current; frozen during Limit State |
| **Primary signal** | NBB (best bid) approaching Upper Band, not trade price vs band |
| **Limit State proxy** | NBB ≥ (Upper Band - buffer); buffer is the Cooper-approved detection margin |
| **Lower band** | Disabled — out of scope per Phase E/F finding |
| **Pre-market** | Strictly `INACTIVE` |
| **Halt timeline** | 15-second Limit State window before actual halt; exit should fire at or before Limit State entry, not after |
