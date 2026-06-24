# Phase LULD-V3b T6 — Duration Sweep Results

**Date:** 2026-06-19
**Labeler:** T5 (5-min arithmetic mean + 1% sticky filter)
**Proximity threshold:** 0.010
**Weights:** w_recall=3.0 / w_fp=1.0 / w_liq=1.0
**Total halts detected by T5 labeler:** 32

## *** HARD STOP ***

The following hard-stop criteria were triggered:
  - max_recall=0.1250 < 0.7
  - min_mean_liq_penalty=34.589 > 0.5

Do not proceed to T6 winner selection or Phase H.
Root cause investigation required before continuing.

## Duration Sweep Table

| dur_sec | n_fires | n_halts | tp | fp | fn | recall | fp_rate | mean_liq_pen | composite |
|--------:|--------:|--------:|---:|---:|---:|-------:|--------:|-------------:|----------:|
|       0 |    1387 |      32 |  0 | 1387 | 32 | 0.0000 |  1.0000 |       44.750 |  -38.9976 |
|       2 |     256 |      32 |  1 | 255 | 31 | 0.0312 |  0.9961 |       41.174 |  -13.4472 |
|       4 |     171 |      32 |  1 | 170 | 31 | 0.0312 |  0.9942 |       43.162 |  -15.2834 |
|       6 |     142 |      32 |  2 | 140 | 30 | 0.0625 |  0.9859 |       34.589 |  -13.5699 |
|       8 |     113 |      32 |  2 | 111 | 30 | 0.0625 |  0.9823 |       42.370 |  -14.7141 |
|      10 |      96 |      32 |  2 | 94 | 30 | 0.0625 |  0.9792 |       45.431 |  -11.9252 |
|      12 |      84 |      32 |  4 | 80 | 28 | 0.1250 |  0.9524 |       41.243 |  -10.1385 |

## V3 Baseline Comparison

| Config | n_fires | n_halts | recall | composite |
|--------|--------:|--------:|-------:|----------:|
| V3 dur=0 (30s VWAP labeler) | 888 | 1 | 0.0000 | -34.1809 |
| V3b dur=0 (T5 labeler) | 1387 | 32 | 0.0000 | -38.9976 |

---

**Cooper selects the winner. Do not proceed to T6 charting or Phase H without explicit approval.**