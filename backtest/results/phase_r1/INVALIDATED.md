# INVALIDATED — Phase R1 Results

**Invalidated:** 2026-06-22  
**Reason:** Scanner hit floor fix not yet applied when these runs were produced.

## What was wrong

All R1 runs (symmetric sweep, asymmetric sweep, T3 per-event charts, D diagnostic) were
produced from the EPG-Rapid runner before the scanner hit floor was added. The runner had
no awareness of when each stock first crossed the 30% momentum threshold that triggers the
live scanner. Entries in 65.4% of events fired before the stock was a valid scanner name.

The entry lag figures, gate chatter counts, and PF/CVaR5 metrics from R1 are therefore
not representative of live-tradeable behavior.

## Affected outputs

| File / Directory | Content | Status |
|-----------------|---------|--------|
| `symmetric_sweep.json` | 6-config p sweep results | **INVALID** |
| `asymmetric_sweep.json` | 8-config asymmetric sweep results | **INVALID** |
| `symmetric_p*/` | Per-config run artifacts | **INVALID** |
| `asymmetric_*/` | Per-config run artifacts | **INVALID** |
| `diagnostic_charts/` | D-diagnostic Plotly charts | **INVALID** |
| `t3_charts_*/` | T3 per-event charts | **INVALID** |
| `_smoke/` | Smoke test runs | **INVALID** |

## What replaces this

R1 will be re-run after Cooper approves the post-fix A4 summary in
`results/scanner_floor_fix/post_fix_summary.json`.

## Audit trail

- `results/warmup_audit/audit_findings.md` — collateral finding, 65.4% pre-scanner entry rate
- `results/scanner_floor_fix/` — post-fix baseline (written after fix approved)
