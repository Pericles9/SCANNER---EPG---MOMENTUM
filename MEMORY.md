# MEMORY.md — scanner-epg-momentum

Read this at session start. Contains discoveries, bugs, and state not in the spec docs.

---

## Bootstrap State (2026-05-04)

Project bootstrapped from `hawkes-ofi-impact` Phase S/T/U. All 49 tests pass. Smoke test
ran without crash (1 event, 0 trades — gap gate filtered the sample event, not a bug).

Key structural choices made during bootstrap:
- `core/events/` from parent → renamed to `core/epg/` (anchor.py, gate.py)
- `_hawkes_replay_with_refit` inlined directly in `backtest/runner.py` (was imported from
  parent's `backtest/runner.py` — would be circular in new project structure)
- `write_json_atomic` and `_json_default` also inlined in `runner.py`
- `core/ofi/trade_ofi.py` copied: needed for Lee-Ready side classification (compute_trade_ofi)
- `core/hawkes/ekf.py` copied: imported by engine.py (KalmanIntensityEstimator)

---

## Phase S Key Findings (from parent project)

- **100% EPG window close exits** in Phase S baseline. EXIT_D and LULD were not active.
- **Pre-market outperforms regular hours:** PF 1.73 vs 1.16 in Phase S.
- **Gap gate blocks 64.5% of PASS edges.** Most EPG windows open but gap < 30%.
- **One trade per PASS window maximum** — by design. After exit mid-window, `prev_state=PASS`
  so next tick is not a rising edge. This is intentional, not a bug.

---

## Phase U Key Findings (from parent project)

- **EXIT_D fires are accretive (PF=1.79)** when theta=0.75, tau_min=8s.
- **LULD fires are destructive (PF≈0, mean=-5.97%)** — 16 fires. Cause TBD.
  Hypothesis: LULD proximity fires just before temporary price extremes that recover,
  or pre-market halt detection mismatch.
- **Pre-market PF regressed 1.73→0.90** when EXIT_D+LULD added. Pre-market flows
  thinner → intensity imbalance noisier → EXIT_D fires prematurely.
- **T10 best combo: theta=0.65, tau_min=4s** → PF=1.3848 (vs default theta=0.75 tau=8s
  PF=1.0962). Default theta was conservative; lowering threshold fires earlier and better.

---

## Open Questions

1. What drives LULD fires being destructive? Gate on pre-market only? Raise proximity threshold?
2. How to gate EXIT_D in pre-market to recover PF 0.90→1.73?
3. Quality of gap-gate queued entries — are they better or worse than immediate entries?
4. T10 theta=0.65 tau=4s on full val split (currently only 100-event sample validated).

---

## Critical Reminders

- **lambda_ref for EPG** = mu_buy + mu_sell from cold-start MLE only. NEVER equilibrium rate.
- **n_base must vary** across trades. Constant n_base = runner is broken (static config loaded).
- **Virtual env:** `D:\Trading Research\.venv\Scripts\python.exe`
- **Test split is locked.** `config/holdout_boundary.json` — do not modify.
- **pytest must pass 49/49** before any backtest run.
