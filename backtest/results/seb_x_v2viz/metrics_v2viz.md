# Phase SEB-X v2-VIZ — Distribution Metrics

## *** GATE C: EV / MEDIAN SIGN DISAGREEMENT (fat-left-tail signature) ***

Stack **B0**: EV=0.38% but median=-3.16% | EV capture=-7.121 median capture=-0.333

Interpretation: a minority of large losers is dragging mean below zero while the median trade is still profitable. This is the fat-left-tail signature. See left-tail contributors in the by-year and by-bucket splits below.

## Stack: B0

### Overall

n=990  EV=0.38%  median=-3.16%  std=14.81%  skew=2.35  kurt=9.20

min=-35.75%  Q1=-8.38%  Q3=5.45%  max=102.52%  IQR=13.83%

%win=38.0%  avg_win=13.63%  avg_loss=-7.75%  payoff=1.758  expectancy=0.37%

PF=1.0799  CVaR5_pct=-19.54%  CVaR5_sigma=-11.467  max_consec_loss=10  max_DD_sum=317.45%  EV/std=0.026

### By Year

| Year | n | EV% | Median% | %win | avg_win | avg_loss | PF | CVaR5% |
|------|---|-----|---------|------|---------|----------|-----|--------|
| 2020 | 159 | 0.51% | -2.26% | 39.6% | 11.60% | -6.85% | 1.123 | -16.57% |
| 2021 | 154 | 1.65% | -2.16% | 41.6% | 13.02% | -6.51% | 1.438 | -13.96% |
| 2022 | 127 | 0.76% | -1.81% | 44.9% | 11.03% | -7.60% | 1.182 | -18.78% |
| 2023 | 185 | -0.75% | -3.17% | 37.8% | 11.83% | -8.42% | 0.856 | -21.85% |
| 2024 | 365 | 0.24% | -4.39% | 33.4% | 17.22% | -8.29% | 1.043 | -20.54% |

### By Session Bucket

| Bucket | n | EV% | Median% | %win | PF | CVaR5% |
|--------|---|-----|---------|------|-----|--------|
| post_market | 40 | -3.22% | -2.34% | 27.5% | 0.319 | -26.62% |
| pre_market | 144 | -0.85% | -5.30% | 32.6% | 0.855 | -22.02% |
| regular_hours | 806 | 0.78% | -2.82% | 39.5% | 1.170 | -18.48% |

### Event Day vs Off-Day

| Split | n | EV% | Median% | PF |
|-------|---|-----|---------|-----|
| event_day | 990 | 0.38% | -3.16% | 1.080 |

## Stack: B0+R1+R3_vwap

### Overall

n=990  EV=1.03%  median=3.06%  std=7.13%  skew=0.30  kurt=1.04

min=-22.77%  Q1=-5.20%  Q3=5.59%  max=37.96%  IQR=10.79%

%win=57.6%  avg_win=6.22%  avg_loss=-6.01%  payoff=1.035  expectancy=1.03%

PF=1.4048  CVaR5_pct=-12.06%  CVaR5_sigma=-2.500  max_consec_loss=6  max_DD_sum=106.33%  EV/std=0.145

### By Year

| Year | n | EV% | Median% | %win | avg_win | avg_loss | PF | CVaR5% |
|------|---|-----|---------|------|---------|----------|-----|--------|
| 2020 | 159 | 1.00% | 2.72% | 60.4% | 5.70% | -6.17% | 1.409 | -11.64% |
| 2021 | 154 | 1.57% | 3.41% | 61.7% | 5.92% | -5.45% | 1.751 | -11.02% |
| 2022 | 127 | 1.31% | 3.36% | 60.6% | 5.97% | -5.87% | 1.567 | -12.53% |
| 2023 | 185 | 1.25% | 3.40% | 60.0% | 6.34% | -6.39% | 1.488 | -11.49% |
| 2024 | 365 | 0.62% | 2.05% | 52.3% | 6.67% | -6.03% | 1.214 | -12.43% |

### By Session Bucket

| Bucket | n | EV% | Median% | %win | PF | CVaR5% |
|--------|---|-----|---------|------|-----|--------|
| post_market | 40 | 0.76% | -1.36% | 40.0% | 1.325 | -10.66% |
| pre_market | 144 | 0.63% | 3.02% | 52.8% | 1.202 | -14.64% |
| regular_hours | 806 | 1.12% | 3.18% | 59.3% | 1.454 | -11.51% |

### Event Day vs Off-Day

| Split | n | EV% | Median% | PF |
|-------|---|-----|---------|-----|
| event_day | 990 | 1.03% | 3.06% | 1.405 |

## Stack: B0+R1+R3_prim

### Overall

n=990  EV=0.75%  median=2.33%  std=6.76%  skew=0.51  kurt=1.87

min=-23.62%  Q1=-4.76%  Q3=4.98%  max=37.89%  IQR=9.74%

%win=55.1%  avg_win=5.85%  avg_loss=-5.52%  payoff=1.060  expectancy=0.75%

PF=1.3036  CVaR5_pct=-11.25%  CVaR5_sigma=-2.500  max_consec_loss=8  max_DD_sum=97.47%  EV/std=0.111

### By Year

| Year | n | EV% | Median% | %win | avg_win | avg_loss | PF | CVaR5% |
|------|---|-----|---------|------|---------|----------|-----|--------|
| 2020 | 159 | 0.72% | 2.38% | 60.4% | 4.70% | -5.35% | 1.338 | -12.61% |
| 2021 | 154 | 1.53% | 3.11% | 61.0% | 5.49% | -4.68% | 1.839 | -8.19% |
| 2022 | 127 | 0.47% | 2.37% | 55.9% | 5.05% | -5.35% | 1.198 | -10.24% |
| 2023 | 185 | 1.21% | 3.17% | 56.8% | 6.29% | -5.45% | 1.515 | -10.96% |
| 2024 | 365 | 0.30% | -1.27% | 49.3% | 6.71% | -5.93% | 1.101 | -11.76% |

### By Session Bucket

| Bucket | n | EV% | Median% | %win | PF | CVaR5% |
|--------|---|-----|---------|------|-----|--------|
| post_market | 40 | 0.39% | 0.17% | 50.0% | 1.163 | -13.61% |
| pre_market | 144 | 0.54% | 1.55% | 51.4% | 1.174 | -16.58% |
| regular_hours | 806 | 0.81% | 2.37% | 56.1% | 1.341 | -9.82% |

### Event Day vs Off-Day

| Split | n | EV% | Median% | PF |
|-------|---|-----|---------|-----|
| event_day | 990 | 0.75% | 2.33% | 1.304 |

---

## Caveats

1. **realized_ret_pct** = (exit_price - entry_price) / entry_price — no slippage/spread.
2. **realized_ret_sigma** = dollar_PnL / sigma_val (consistent with stop calibration).
   Note: v2 sweep.parquet stored pnl_frac/sigma_dollar (different unit) — CVaR5 there is not comparable.
3. **Tier 0 empty** — loss distributions under-weight real faders; left tail likely thinner here than live.
4. **No true holdout.** Tune/confirm split is temporal 70/30 on the same 990 entries.