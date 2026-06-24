# Phase AUDIT-RAPID — Complete Runner Logic Audit (Findings)

**Date:** 2026-06-24
**Mode:** Read-only. No code changes, no parameter changes, no backtest runs.
**Primary target:** `backtest/runner_rapid.py`
**Per-event charts:** Not produced. This is an analysis-only phase with no backtest run and
no trade records — exempt per `Agent_Prompt_Standard.md §7`. Stated explicitly here as required.

---

## Path reconciliation (prompt paths → actual paths)

Several files referenced in the prompt Context are present but at slightly different paths.
None of these triggered the escalation criteria (no required file is genuinely missing).

| Prompt path | Actual path | Note |
|---|---|---|
| `backtest/docs/EPG_Rapid_Strategy.md` | `backtest/docs/EPG_Rapid_Strategy (1).md` | `(1)` download suffix |
| `backtest/docs/EPG_Rapid_Test_Phases.md` | `backtest/docs/EPG_Rapid_Test_Phases (1).md` | `(1)` download suffix |
| `backtest/data/val_mdr150_diagnostic.json` | `D:\Trading Research\data\val_mdr150_diagnostic.json` | lives outside repo at `DATA_ROOT`; runner loads via `DATA_ROOT` and the catalog, not from `backtest/data/` |
| `results/phase_diag_gate/replay_data/LRHC_2024-06-14.json` | `backtest/results/phase_diag_gate/replay_data/LRHC_2024-06-14.json` | prompt paths are relative to `backtest/` |
| `results/audit_rapid/` (output) | `backtest/results/audit_rapid/` | prompt paths are relative to `backtest/` |

**Authoritative-source note for the whole audit:** The spec doc `EPG_Rapid_Strategy (1).md`
contains an internal contradiction on LULD. §2 (Locked Decisions) says **"LULD proximity —
Disabled — Not in EPG-Rapid exit stack"**, while §4/§5/§10 still describe LULD as exit #1.
`backtest/CLAUDE.md`, `EPG_Rapid_Test_Phases (1).md` Cross-Phase Notes ("LULD: not in EPG-Rapid
exit stack (dropped). C2 and R4 dropped."), and commit `5615191` ("drop LULD from exit stack;
C2+R4 DROPPED") all resolve this: **LULD is dropped from the EPG-Rapid exit stack.** This audit
treats "LULD dropped" as the correct spec for T4/T7. §4/§5/§10 of the strategy doc are stale.

---

# T1 — Scanner Hit Simulation

Files read: `D:\Trading Research\data\val_mdr150_diagnostic.json`,
`backtest/scripts/build_scanner_hit_catalog.py`, `backtest/scripts/build_mdr200_sample.py`,
`backtest/runner_rapid.py`.

### T1a — Field provenance

> **Spec says:** Each event must carry a `t_scanner_hit_sec` (or equivalent) representing when the scanner fired (`EPG_Rapid_Strategy.md §3.0`).
> **Code does:** The val sample has no literal `t_scanner_hit_sec`; it carries equivalents `scanner_hit_ts_ns`, `scanner_hit_tod_sec`, `scanner_hit_price`, `scanner_hit_idx`, `prev_close`. The runner derives its working `scanner_hit_t_sec` from `scanner_hit_ts_ns`.
> **Match:** ✅ YES (equivalent field present for every event)
> **Evidence:**
> First 3 event records (`val_mdr150_diagnostic.json`), field names + values:
> ```
> EVENT 0  SGD 2023-11-17  keys=[ticker,date,mom_pct,dir_name,has_quotes,
>   scanner_hit_ts_ns, scanner_hit_tod_sec, scanner_hit_price, scanner_hit_idx, prev_close]
>   scanner_hit_ts_ns=1700211646191523346  scanner_hit_tod_sec=46.192  scanner_hit_price=6.38  scanner_hit_idx=652  prev_close=4.9
> EVENT 1  ASST 2023-11-27
>   scanner_hit_ts_ns=1701090134038579705  scanner_hit_tod_sec=14534.039  scanner_hit_price=0.5  scanner_hit_idx=19  prev_close=0.3568
> EVENT 2  BDRX 2023-11-27
>   scanner_hit_ts_ns=1701093681245450688  scanner_hit_tod_sec=18081.245  scanner_hit_price=3.66  scanner_hit_idx=194  prev_close=2.7901
> ```
> The runner converts ns → its own t_sec frame, `runner_rapid.py:472`:
> ```python
> scanner_hit_t_sec = (_sh_ts_ns - int(td.timestamps[0])) / NS_PER_SECOND
> ```
> Note: the runner reads `scanner_hit_ts_ns` from the **catalog** (work item), not from the event
> file, `runner_rapid.py:1179`:
> ```python
> "scanner_hit_ts_ns": catalog_rec.get("scanner_hit_ts_ns") if catalog_rec else None,
> ```
> The event-file copy and the catalog copy agree because the sample was built from the catalog
> (`build_mdr200_sample.py:75-82`).

### T1b — How was it computed?

> **Spec says:** `t_scanner_hit_sec` is "the timestamp when `todaysChangePerc ≥ 30%` was first satisfied" (`EPG_Rapid_Strategy.md §3.0`).
> **Code does:** Option **(b)** — the first tick in the trade data where `price >= prev_close × 1.30`, computed from tick data. NOT from stored scanner poll timestamps.
> **Match:** ✅ YES (consistent with the conceptual definition; method is tick-data-derived)
> **Evidence:** `build_scanner_hit_catalog.py:93,103-114`:
> ```python
> threshold = prev_close * (1.0 + SCANNER_THRESHOLD)   # SCANNER_THRESHOLD = 0.30
> ...
> for i in range(td.n_trades):
>     if td.prices[i] >= threshold:
>         hit_ts_ns = int(td.timestamps[i])
>         hit_tod   = float(td.timestamps[i] - start_ns) / NS_PER_SEC
>         hit_price = float(td.prices[i])
>         hit_idx   = i
>         ...
>         break
> ```

### T1c — Re-cross handling

> **Spec says:** First appearance is first, full stop — first crossing, not the latest (`EPG_Rapid_Strategy.md §3.1`).
> **Code does:** Uses the FIRST crossing — the loop `break`s on the first trade ≥ threshold and never revisits.
> **Match:** ✅ YES
> **Evidence:** `build_scanner_hit_catalog.py:103-114` (the `break` on first qualifying trade, quoted above). No re-cross / latest-crossing logic exists.

### T1d — Reference price for the 30% threshold

> **Spec says:** Threshold computed against a reference (`todaysChangePerc`).
> **Code does:** Uses the **official prior-day close** via `get_prev_close()` (3-source chain: DuckDB daily_bars → `daily/{TICKER}_daily.parquet` → last trade of prior event-day). NOT session open, NOT first tick of the day.
> **Match:** ✅ YES
> **Evidence:** `build_scanner_hit_catalog.py:81,93`:
> ```python
> prev_close = get_prev_close(ticker, date)
> ...
> threshold = prev_close * (1.0 + SCANNER_THRESHOLD)
> ```
> Module docstring (`build_scanner_hit_catalog.py:7-10`): "Uses data.loaders.prev_close.get_prev_close (same 3-source chain as runner_rapid): 1. DuckDB daily_bars 2. data/daily/{TICKER}_daily.parquet 3. Last trade from prior event-day directory". The runner uses the same `get_prev_close` at `runner_rapid.py:442`.

### T1e — Scanner cadence simulation

> **Spec says:** Live scanner polls every 15–30s, so `t_scanner_hit_sec` could lag the true crossing by up to 30s (`EPG_Rapid_Strategy.md §1`, R3 context "actual poll cadence 15–30s"). Document the method (not pass/fail).
> **Code does:** Uses the **exact tick timestamp** of the first crossing trade. No 15–30s poll-cadence rounding/lag is simulated — detection is treated as instantaneous at the crossing tick.
> **Match:** ⚠️ PARTIAL — not a spec violation, but an optimistic realism gap that must be documented.
> **Evidence:** `build_scanner_hit_catalog.py:105` stores `hit_ts_ns = int(td.timestamps[i])` — the raw trade timestamp, with no poll-grid snapping. Consequence: backtest entry can fire up to ~30s earlier than a live 15–30s-poll scanner would have allowed. This advantages every event's entry timing relative to production.

---

# T2 — Scanner Floor Guard Placement

Files read: `backtest/runner_rapid.py`.

### T2a — Absolute position of the floor check in the loop

> **Spec says:** The scanner floor (`t_sec ≥ t_scanner_hit`) is "always the first guard inside the entry loop" (`EPG_Rapid_Strategy.md §3, §3.0`).
> **Code does:** The floor check IS the first conditional in the entry/exit loop (right after reading `cur`). BUT note: gate state for every tick is computed earlier in a **separate precompute loop** (lines 543–554) that has no floor — so "before any call to `gate.update()`" is false (see T2b).
> **Match:** ✅ YES (for entry-loop placement) / see T2b for gate-update ordering.
> **Evidence:** `runner_rapid.py:582-594`:
> ```python
> for i in range(N):
>     cur = epg_states[i]
>     # Scanner hit floor: no entry processing before first scanner hit tick
>     if scanner_hit_t_sec is not None and td.t_sec[i] < scanner_hit_t_sec:
>         prev_state = cur
>         continue
>     # max_entry_lag_sec filter: once lag exceeded, no entry is possible
>     max_lag = args.get("max_entry_lag_sec")
>     if (max_lag is not None and scanner_hit_t_sec is not None
>             and td.t_sec[i] - scanner_hit_t_sec > max_lag):
>         break
> ```

### T2b — Pre-floor gate updates

> **Spec says:** Gate is intentionally "replayed through all pre-event history from 4:00am, so it is already warm — and often in PASS state — at the scanner hit tick" (`EPG_Rapid_Strategy.md §3.0`).
> **Code does:** YES — pre-scanner ticks feed the gate. `gate.update()` runs for ALL N ticks in the precompute loop (no floor there); the peak ratchet and `lambda_V` are built on pre-scanner price action. This is **by design** per the spec (the floor blocks entry only, not gate warming).
> **Match:** ✅ YES (intended behavior, matches spec)
> **Evidence:** `runner_rapid.py:543-554` (the gate precompute loop runs over `range(N)` with no scanner-floor guard):
> ```python
> for i in range(N):
>     t_ev = anchor.update(lambda_hat[i], td.t_sec[i])
>     if t_ev is not None and not t_event_fired:
>         gate.activate(t_ev)
>         ...
>     dv = float(td.prices[i]) * float(td.sizes[i])
>     epg_states[i] = gate.update(dv, td.t_sec[i])
>     if (scanner_hit_t_sec is not None and gate_at_scanner_hit is None
>             and td.t_sec[i] >= scanner_hit_t_sec):
>         gate_at_scanner_hit = epg_states[i].name
> ```

### T2c — Floor variable source

> **Spec says:** Floor read from the scanner hit catalog; events with no confirmed hit are skipped.
> **Code does:** `scanner_hit_t_sec` is derived from `scanner_hit_ts_ns`, loaded from the catalog into the work item; non-null for every val event (the MDR≥150 universe was filtered on `scanner_hit_ts_ns IS NOT NULL`).
> **Match:** ✅ YES
> **Evidence:** Work item population, `runner_rapid.py:1140-1141,1179-1180`:
> ```python
> catalog_rec = scanner_hit_catalog.get(catalog_key)  # None if not in catalog
> ...
> "scanner_hit_ts_ns": catalog_rec.get("scanner_hit_ts_ns") if catalog_rec else None,
> "scanner_hit_in_catalog": catalog_rec is not None,
> ```
> Worker resolves it, `runner_rapid.py:466-472`:
> ```python
> _sh_ts_ns       = args.get("scanner_hit_ts_ns")          # int | None
> _sh_in_catalog  = args.get("scanner_hit_in_catalog", False)
> scanner_hit_t_sec: float | None = None
> if _sh_in_catalog:
>     if _sh_ts_ns is None:
>         return {**base, "status": "skipped", "reason": "no_scanner_hit"}
>     scanner_hit_t_sec = (_sh_ts_ns - int(td.timestamps[0])) / NS_PER_SECOND
> ```
> The MDR≥150 build guarantees a hit for every event (`build_mdr200_sample.py:72-82` filters on `scanner_hit_ts_ns is not None`), so `scanner_hit_t_sec` is never None/0 for the val sample.

---

# T3 — Entry State Machine

Files read: `backtest/runner_rapid.py`, `backtest/core/filters/rapid_entry.py`.

### T3a — Rising-edge check in the rapid entry path

> **Spec says:** `entry_mode=rapid` (first_pass) enters on the first tick where `gate.state == PASS` at/after scanner hit — **no rising-edge requirement** (`EPG_Rapid_Strategy.md §1,§3`; `EPG_Rapid_Test_Phases.md R0 T2`).
> **Code does:** The `first_pass` branch has NO rising-edge guard — it is a pure state check `if cur == GateState.PASS`. The rising-edge guard exists only in the separate `rising_edge` (classic baseline) branch.
> **Match:** ✅ YES (no rising-edge in rapid path; the bimodal lag is expected, not a copied guard)
> **Evidence:** `runner_rapid.py:607-617`:
> ```python
> if entry_mode == "rising_edge":
>     # Classic: rising-edge only; zero SF involvement (§1.1 constraint)
>     if (cur == GateState.PASS and
>             prev_state in (GateState.INACTIVE, GateState.WARMUP,
>                            GateState.FAIL)):
>         n_pass_edges += 1
>         entry_accepted = True
> elif entry_mode == "first_pass":
>     # First-PASS (EPG-Rapid §1): enter on first live PASS tick.
>     # No rising-edge requirement, no SF, no entry_eligible(), no n_hold.
>     if cur == GateState.PASS:
>         entry_accepted = True
> ```
> Note on the bimodal lag (median 0s / mean 58s): in `first_pass`, entry fires immediately when
> the gate is already PASS at scanner hit (lag≈0), and waits to the next PASS tick when the gate
> is FAIL at scanner hit (the lag tail). LRHC is a concrete tail case — gate was FAIL (ratio
> 0.6999) at scanner hit and entered 11.4s later at the first PASS (see T4f). This is the
> expected first_pass distribution, **not** a rising-edge artifact.

### T3b — `entry_eligible()` presence

> **Spec says:** SF cross-and-hold / `entry_eligible()` was removed from the rapid entry path (`EPG_Rapid_Test_Phases.md R0 T2a`: "No `entry_eligible()` call anywhere in the rapid entry path").
> **Code does:** `entry_eligible()` is never called. Only `Q_THRESHOLD` is imported from `rapid_entry.py`. The legacy `cross_and_hold` mode does an inline `q_tilde`/`n_hold` check (not a call to `entry_eligible`), and a hard assertion guarantees `rising_edge`/`first_pass` never touch it.
> **Match:** ✅ YES (absent from the rapid path)
> **Evidence:** Import, `runner_rapid.py:75`: `from core.filters.rapid_entry import Q_THRESHOLD` (function `entry_eligible` not imported). The `cross_and_hold` inline check, `runner_rapid.py:619-628`, never calls `entry_eligible`. Hard guard, `runner_rapid.py:728-733`:
> ```python
> if entry_mode in ("rising_edge", "first_pass") and n_entry_eligible_blocks != 0:
>     raise AssertionError(
>         f"[§1.1] {entry_mode} mode called entry_eligible {n_entry_eligible_blocks}x ...")
> ```

### T3c — Gap gate

> **Spec says:** Rapid runner should not have a gap gate — scanner hit already confirms the 30% gap (`EPG_Rapid_Strategy.md §3`).
> **Code does:** Gap-check code exists but is inert in rapid mode: `gap_gate_enabled` is forced `False` for non-parity runs, so the blocking branch never executes (the `else` only computes `intraday_pct` for reporting).
> **Match:** ✅ YES (code present but disabled by default in rapid mode)
> **Evidence:** Forced off, `runner_rapid.py:1131-1134`:
> ```python
> else:
>     # Rapid: gap gate OFF by default (scanner already ensures >=30%).
>     gap_gate_enabled = False
> ```
> Worker, `runner_rapid.py:633-640`:
> ```python
> if gap_gate_enabled:
>     intraday_pct = (cur_price - prev_close) / prev_close
>     if intraday_pct < gap_threshold:
>         n_gap_gate_blocks += 1
>         prev_state = cur
>         continue
> else:
>     intraday_pct = (cur_price - prev_close) / prev_close
> ```

### T3d — `max_entry_lag_sec` behavior

> **Spec says:** `max_entry_lag_sec` is "maximum allowed seconds from scanner hit to entry … applied per-event as a hard filter … events where `t_entry − t_scanner_hit > max_entry_lag_sec` are excluded from scoring." It is an **entry/scoring invalidation deadline only** (`EPG_Rapid_Strategy.md §2.1`). The runner should abandon the event if no entry fires within the deadline; it must NOT wait before attempting entry, and it must NOT affect an already-open position.
> **Code does:** Implemented as an unconditional **`break` of the entire tick loop** placed before the entry/exit dispatch. For entry, this is correct (it does not wait — it processes entries from scanner hit, and abandons once the deadline passes). **But the same `break` also terminates exit monitoring of an already-open position** — see T4e for the material consequence.
> **Match:** ⚠️ PARTIAL — the entry-side semantics are correct (abandon, don't wait), but the implementation as a loop `break` produces an incorrect exit-side side effect (T4e).
> **Evidence:** `runner_rapid.py:590-594`:
> ```python
> max_lag = args.get("max_entry_lag_sec")
> if (max_lag is not None and scanner_hit_t_sec is not None
>         and td.t_sec[i] - scanner_hit_t_sec > max_lag):
>     break
> ```
> This `break` sits at lines 590–594, before the `if not in_position … elif in_position` dispatch at lines 604/649, so it preempts both entry and exit on every tick past the deadline.

### T3e — `closed_today` flag timing

> **Spec says:** `closed_today=True` set at entry, before fill confirmation (`EPG_Rapid_Strategy.md §2`, Re-entry row).
> **Code does:** `closed_today` initialized `False`; set `True` inside the entry-accepted block at the moment of entry (immediately after `in_position=True`), not at scanner hit or context fetch.
> **Match:** ✅ YES
> **Evidence:** Init, `runner_rapid.py:569` (`closed_today = False`). Set at entry, `runner_rapid.py:642-647`:
> ```python
> entry_price = float(td.prices[min(i + 1, N - 1)])
> entry_idx = i
> entry_t_sec = td.t_sec[i]
> intraday_pct_at_entry = intraday_pct
> in_position = True
> closed_today = True  # hard re-entry off
> ```

---

# T4 — Exit State Machine

Files read: `backtest/runner_rapid.py`,
`backtest/results/phase_diag_gate/replay_data/LRHC_2024-06-14.json`,
`backtest/results/phase_diag_gate/selected_events.json`.

### T4a — Exit check placement relative to `gate.update()`

> **Spec says:** Exit fires on PASS→FAIL/INACTIVE; transition must be detected against the prior tick (`EPG_Rapid_Strategy.md §4`).
> **Code does:** All `gate.update()` calls happen in a **separate precompute loop** (lines 543–554) that fills `epg_states[]`. The entry/exit loop then reads the precomputed `cur = epg_states[i]` and a local `prev_state` carried from the prior tick. So the exit check is evaluated against fully-precomputed states; `prev_state` is the prior tick's gate state (no same-tick double update, no stale gate object).
> **Match:** ✅ YES
> **Evidence:** Precompute, `runner_rapid.py:551` (`epg_states[i] = gate.update(dv, td.t_sec[i])`). Exit-loop read + transition, `runner_rapid.py:583,649-651`:
> ```python
> cur = epg_states[i]
> ...
> elif in_position:
>     # EPG window close: PASS → not-PASS
>     if prev_state == GateState.PASS and cur != GateState.PASS:
> ```

### T4b — Transition detection vs. state check

> **Spec says:** Exit fires on the PASS→FAIL/INACTIVE transition (`EPG_Rapid_Strategy.md §4`).
> **Code does:** Transition detection — `prev_state == PASS and cur != PASS`, not a bare `cur == FAIL`.
> **Match:** ✅ YES
> **Evidence:** `runner_rapid.py:651`:
> ```python
> if prev_state == GateState.PASS and cur != GateState.PASS:
> ```

### T4c — `in_position` guard

> **Spec says:** Exit applies to an open position only.
> **Code does:** The exit check is inside `elif in_position:` (mutually exclusive with the entry branch), so it cannot run before a position exists and cannot fire on the same tick as entry.
> **Match:** ✅ YES
> **Evidence:** `runner_rapid.py:604,649-651`:
> ```python
> if not in_position and not closed_today:
>     ... entry ...
> elif in_position:
>     if prev_state == GateState.PASS and cur != GateState.PASS:
>         ... exit (epg_window_close) ...
> ```

### T4d — `epg_window_close.enabled` config flag

> **Spec says:** `epg_window_close: { enabled: true }` (`EPG_Rapid_Strategy.md §10`); config `epg_window_close_exit.enabled = true`.
> **Code does:** The config flag is `true` in `config/strategy.json`, but **`runner_rapid.py` never reads it**. The EPG-window-close exit is hard-coded ON in rapid mode (no `if enabled:` gate). Net behavior matches the spec intent, but the flag is not honored — disabling it in config would have no effect.
> **Match:** ⚠️ PARTIAL — correct behavior, but flag is ignored (config divergence).
> **Evidence:** Config, `config/strategy.json:50-53`:
> ```json
> "epg_window_close_exit": { "enabled": true, "description": "EXIT when EPG PASS window closes ..." }
> ```
> A grep of `runner_rapid.py` for `epg_window_close_exit` returns only the hard-coded exit-reason string at line 680 — the `enabled` flag is never loaded or checked. The exit at lines 649-651 runs unconditionally whenever `in_position`.

### T4e — Session-end check ordering  ❌ HEADLINE FINDING

> **Spec says:** Exit stack first-wins per trade tick; session end is the last-resort fallback. PASS→FAIL exit (EPG window close) should win whenever the gate closes before the true end of data (`EPG_Rapid_Strategy.md §4`).
> **Code does:** The relative ordering is correct — the in-loop `epg_window_close` check runs first, and the `session_end` exit is a post-loop fallback (`if in_position:` after the loop). **However, the `max_entry_lag_sec` `break` (T3d) preempts the entire exit stack.** When `max_entry_lag_sec` is set (R1+ runs use 300s), the loop `break`s at the first tick past `scanner_hit + max_lag`. Any position still open at that point stops being monitored for `epg_window_close`, falls through to the post-loop block, and is booked as **`session_end` at the final tick price** (`td.prices[N-1]`, `td.t_sec[N-1]`). This force-converts otherwise-EPG-close exits into session-end exits and books PnL at end-of-data rather than where the gate actually closed.
> **Match:** ❌ NO (the `break` short-circuits the exit stack; ordering is moot once the loop terminates early)
> **Evidence:** The preemptive `break`, `runner_rapid.py:590-594` (quoted in T3d), executes before the exit dispatch at line 649. Post-loop session_end fallback, `runner_rapid.py:691-722`:
> ```python
> # ── If still in position at session end ──
> if in_position:
>     exit_price = float(td.prices[N - 1])
>     exit_t_sec = td.t_sec[N - 1]
>     ...
>     "exit_reason": "session_end",
> ```
> **Scope:** This only fires when `max_entry_lag_sec is not None`. R0 (`max_entry_lag_sec=null`) is
> unaffected — the `break` is guarded by `max_lag is not None`. R1 (Cooper set 300s; see
> `backtest/CLAUDE.md` R1 row) is affected, and the DIAG-GATE-2 baseline that motivated this audit
> ran at `MAX_LAG_SEC=300` / `P_CLOSE=0.70` (`diag_gate2_charts.py:35-36`).
>
> **This directly explains the DIAG-GATE-2 observation "4/5 exits are session_end despite
> transitions."** Of the 5 diagnostic events (`selected_events.json`):
> - **LRHC** (slot A): entered at scanner+11.4s; its gate went PASS→FAIL **0.13s later** (inside the
>   300s window) → `epg_window_close`. ✅ caught.
> - **MLGO** (B, lag 0), **MBIO** (C, lag 0), **VSSYW** (D, lag 0), **BDRX** (E, lag ≈261s): all
>   entered within 300s but their first post-entry PASS→FAIL fell **after** `scanner+300s`, so the
>   loop broke while in position → `session_end` booked at the final tick. PnL: +203%, +150.9%,
>   −88.2%, +26.4% respectively — i.e. these were held to absolute end-of-data with the gate exit
>   disabled.
>
> **Implication for R1:** because the dominant exit for kept trades is `session_end` at end-of-data
> (independent of `p_close`), the R1 gate-threshold sweep — whose stated purpose is to recalibrate
> `p_close`, which "directly controls exit timing" (`EPG_Rapid_Strategy.md §2.1`) — is measuring
> hold-to-end-of-data PnL for most trades, not gate-driven exits. The sweep is largely decoupled
> from the parameter it is tuning.

### T4f — LRHC exit-tick state

> **Spec says:** Exit fires on PASS→FAIL transition.
> **Code does / data shows:** At LRHC's recorded exit (`exit_t_sec=36068.090`, `exit_ts=1718389484551334717`), the nearest replay tick is idx 69377, `gate_state=FAIL` (ratio 0.69991); the prior tick idx 69376 is `PASS` (ratio 0.70071). So the runner caught a genuine PASS→FAIL transition — the gate ratio dipped just below `p_close=0.70`. Entry was idx 69374 (`PASS`, ratio 0.70008) at +11.4s after scanner; the prior tick idx 69373 was `FAIL` (ratio 0.69999900). Hold = 0.13s.
> **Match:** ✅ YES (transition correctly caught)
> **Evidence:** Extracted from `replay_data/LRHC_2024-06-14.json` (n_ticks=116130; scanner_hit_t_sec=36056.551):
> ```
> idx=69373 t_sec=36067.958 gate_state=FAIL ratio=0.6999990   <- prior to entry
> idx=69374 t_sec=36067.958 gate_state=PASS ratio=0.7000804   <- ENTRY (first PASS after scanner)
> idx=69375 t_sec=36067.965 gate_state=PASS ratio=0.7000717
> idx=69376 t_sec=36067.967 gate_state=PASS ratio=0.7000706   <- prior tick to exit
> idx=69377 t_sec=36068.090 gate_state=FAIL ratio=0.6999134   <- EXIT (PASS->FAIL), epg_window_close
> Total PASS->notPASS transitions across the LRHC replay: 16
> ```
> **Pattern established:** LRHC succeeded because its first post-entry PASS→FAIL occurred 0.13s
> after entry — far inside the 300s `max_lag` window. The gate chatters 16 times over the event;
> for the four `session_end` events, the first post-entry PASS→FAIL did not occur before
> `scanner+300s`, so the T4e `break` cut exit monitoring off before any chatter could trigger an
> `epg_window_close`. The pattern LRHC caught (a near-threshold ratio dip) exists in all events,
> but only LRHC's happened to fall inside the deadline window.

---

# T5 — Gate Instantiation and Wiring

Files read: `backtest/runner_rapid.py`, `backtest/core/epg/gate.py`.

### T5a — `activate()` call site and count

> **Spec says:** `activate(t_event)` called once during historical replay (`EPG_Rapid_Strategy.md §2`).
> **Code does:** Exactly once per event — guarded by the `t_event_fired` flag inside the precompute loop; never re-armed on new PASS windows or at scanner hit.
> **Match:** ✅ YES
> **Evidence:** `runner_rapid.py:543-549`:
> ```python
> for i in range(N):
>     t_ev = anchor.update(lambda_hat[i], td.t_sec[i])
>     if t_ev is not None and not t_event_fired:
>         gate.activate(t_ev)
>         t_event_fired = True
>         t_event_idx = i
>         t_event_sec = td.t_sec[i]
> ```
> No other `gate.activate(` call exists in `runner_rapid.py`.

### T5b — `update()` argument correctness

> **Spec says:** `update(dollar_vol, timestamp)` per tick with `dollar_vol = price * size` (`EPG_Rapid_Strategy.md §2`).
> **Code does:** Passes `dv = price * size` and tick-domain `td.t_sec[i]` (not `size` alone, not `price` alone, not `time.time()`).
> **Match:** ✅ YES
> **Evidence:** `runner_rapid.py:550-551`:
> ```python
> dv = float(td.prices[i]) * float(td.sizes[i])
> epg_states[i] = gate.update(dv, td.t_sec[i])
> ```

### T5c — Timestamp domain consistency

> **Spec says:** `update()` timestamp must share the time domain of `t_event` passed to `activate()`.
> **Code does:** Both use the `td.t_sec` frame (seconds since first trade). `activate(t_ev)` receives `t_ev` from `anchor.update(lambda_hat[i], td.t_sec[i])` (anchor works in `td.t_sec`), and `update(dv, td.t_sec[i])` uses the same array. Consistent — no ns/seconds mismatch.
> **Match:** ✅ YES
> **Evidence:** `runner_rapid.py:544` (`anchor.update(..., td.t_sec[i])` → `t_ev`) and `:546` (`gate.activate(t_ev)`) and `:551` (`gate.update(dv, td.t_sec[i])`). Gate consumes them as a single domain in peak mode, `gate.py:398` (`dt = timestamp - self._last_timestamp`).

### T5d — `prev_gate_state` tracking

> **Spec says:** Transition detection requires a correctly carried prior state (feeds T4b).
> **Code does:** `prev_state` is a single local variable initialized once to `INACTIVE`, then assigned `prev_state = cur` at the end of each iteration AND on every `continue` path (scanner floor, gap-gate block). It is never reset mid-loop, so transition detection is correct.
> **Match:** ✅ YES
> **Evidence:** Init, `runner_rapid.py:574` (`prev_state = GateState.INACTIVE`). End-of-iter assignment, `:688` (`prev_state = cur`). Continue-path assignments, `:587` (scanner floor) and `:637` (gap-gate block). No reset-to-INACTIVE inside the loop body.

---

# T6 — Halt Handling Wiring

Files read: `backtest/runner_rapid.py`, `backtest/runner.py` (`_hawkes_replay_with_refit`),
`backtest/core/epg/gate.py`, `backtest/core/features/luld_halt_detection.py`.

### T6a — `detect_luld_halts()` call and forwarding

> **Spec says:** `detect_luld_halts()` produces a `HaltWindow` list, forwarded to `_hawkes_replay_with_refit` as `halt_windows` (`EPG_Rapid_Strategy.md §6`; `EPG_Rapid_Test_Phases.md C3`).
> **Code does:** Called once per event via `_build_halt_intervals(td)`; the resulting interval list is forwarded into the replay as `halt_intervals` (not computed-and-discarded).
> **Match:** ✅ YES
> **Evidence:** Call + forward, `runner_rapid.py:493,510-518`:
> ```python
> halt_intervals = _build_halt_intervals(td)
> ...
> cold_start_params = _hawkes_replay_with_refit(
>     ..., halt_intervals=halt_intervals or None,
> )
> ```
> `_build_halt_intervals`, `runner_rapid.py:365-388`, calls `detect_luld_halts(trades_df, price_col="price")` at line 376 and returns `(start_sec, end_sec)` tuples in the `t_sec` frame.

### T6b — Gate `dt` substitution scope  ❌

> **Spec says:** "Hawkes EMA / gate `λ_V`: for any trade pair straddling a halt gap … substitute `dt = 0` (or epsilon) … so the intensity **and `λ_V`** do not collapse across the suspension." Both the Hawkes EMA and the gate `λ_V` must get the substitution (`EPG_Rapid_Strategy.md §6`; `EPG_Rapid_Test_Phases.md C3 T3c`).
> **Code does:** The `dt_eff = 1e-6` substitution is applied **only inside `_hawkes_replay_with_refit`**, to the Hawkes EMA decay (`R_buy`/`R_sell`). The gate's `λ_V` is updated separately by `gate.update(dv, td.t_sec[i])` using raw timestamps; peak mode computes its **own** `dt = timestamp - last_timestamp` with no halt awareness and applies full exponential decay across the gap. The gate's `is_halted` parameter is honored only in `cusum`/`bocpd` modes, and `runner_rapid` never passes it anyway. So the gate `λ_V` **does** collapse across halts — the spec's gate-side substitution is not implemented.
> **Match:** ❌ NO
> **Evidence:** Substitution scoped to the Hawkes EMA only, `runner_rapid.py:319-330` (inside `_hawkes_replay_with_refit`):
> ```python
> dt = t_sec[i] - t_sec[i - 1]
> dt_eff = dt
> if _halt_ivs and dt_eff > halt_gap_threshold:
>     t_prev, t_curr = t_sec[i - 1], t_sec[i]
>     for h_s, h_e in _halt_ivs:
>         if t_prev < h_e and t_curr > h_s:
>             dt_eff = 1e-6
>             break
> if dt_eff > 0:
>     decay = np.exp(-params.beta * dt_eff)   # <-- Hawkes R_buy/R_sell only
>     R_buy *= decay
>     R_sell *= decay
> ```
> Gate update with raw timestamps and no halt info, `runner_rapid.py:551`:
> ```python
> epg_states[i] = gate.update(dv, td.t_sec[i])   # no halt_intervals, no is_halted
> ```
> Peak-mode gate computes its own dt and decays λ_V unconditionally, `gate.py:398-404`:
> ```python
> dt = timestamp - self._last_timestamp
> if dt < 0:
>     dt = 0.0
> decay = math.exp(-self._decay_rate * dt)
> self._lambda_v = self._lambda_v * decay + dollar_vol * self._decay_rate
> ```
> `is_halted` is consulted only in `_update_cusum`/`_update_bocpd` (`gate.py:551,671`), not in peak mode — and `runner_rapid` runs `gate_mode="peak"` (config `epg.gate_mode="peak"`).

### T6c — `halt_gap_threshold` consistency

> **Spec says:** C3 substitution threshold is 60s (`EPG_Rapid_Test_Phases.md C3 T2`, `EPG_Rapid_Strategy.md §10` `halt.gap_seconds: 60`).
> **Code does:** The substitution check uses 60s (`HALT_GAP_THRESHOLD = 60.0`, matching C3). But `detect_luld_halts()` is called with its **default `halt_gap_seconds=300`** (the gap required to *confirm* a halt window). So the two stages use different gap thresholds: halts are only detected when a gap ≥300s exists, while the dt substitution fires on any gap >60s overlapping a (300s-confirmed) window. The 60s substitution value matches the C3 spec; the 300s detection value is a separate, larger parameter and is not reconciled with the 60s figure.
> **Match:** ⚠️ PARTIAL — substitution threshold (60s) matches C3 spec; detection threshold (300s, default) differs and is not aligned.
> **Evidence:** Substitution threshold, `runner_rapid.py:98` (`HALT_GAP_THRESHOLD = 60.0`) and `:199` (`halt_gap_threshold: float = HALT_GAP_THRESHOLD`). Detection threshold (default), `core/features/luld_halt_detection.py:91`:
> ```python
> def detect_luld_halts(
>     ...
>     halt_gap_seconds: int = 300,
> ```
> `_build_halt_intervals` calls `detect_luld_halts(trades_df, price_col="price")` (`runner_rapid.py:376`) with no `halt_gap_seconds` override → uses the 300s default. (Practical effect: with a 300s detection floor and a 60s substitution floor, the substitution floor is non-binding — any gap large enough to define a window already exceeds 60s.)

---

# T7 — Session Boundary and Exit Stack Completeness

Files read: `backtest/runner_rapid.py`.

### T7a — Session-end timestamp

> **Spec says:** Not explicitly fixed for the rapid fallback; `config.session` lists `rth_end 16:00` and `session_end 20:00`.
> **Code does:** The `session_end` exit uses the **last available trade tick** (`td.t_sec[N-1]` / `td.prices[N-1]`), not a hard 16:00 or 20:00 cutoff. `RTH_END_SEC=43200` (12:00 from a 4:00 origin = 16:00 ET) is used only for `session_bucket()` labeling, not for the exit.
> **Match:** ✅ YES (documented: fallback exit = last available tick)
> **Evidence:** Post-loop fallback, `runner_rapid.py:692-693`:
> ```python
> exit_price = float(td.prices[N - 1])
> exit_t_sec = td.t_sec[N - 1]
> ```
> Labeling-only constant, `runner_rapid.py:90-91,166-171` (`RTH_END_SEC = 43200.0`; `session_bucket`).

### T7b — Complete exit stack in code

> **Spec says (resolved):** EPG-Rapid exit stack = EPG window close (LULD dropped, EXIT_D off). See the authoritative-source note at the top.
> **Code does:** While `in_position`, the only in-loop exit is `epg_window_close` (PASS→FAIL). The only other exit is the post-loop `session_end` fallback. No LULD proximity exit and no EXIT_D are evaluated in rapid mode.
> **Match:** ✅ YES (matches the resolved spec; `session_end` is the implicit fallback)
> **Evidence:** In-loop exit, `runner_rapid.py:649-680` (`epg_window_close` only). Post-loop fallback, `runner_rapid.py:691-722` (`session_end`). No `ProximityState`/`LuldProximityExit`/EXIT_D evaluation exists anywhere in `_process_event_rapid`. (The stale strategy-doc §4 lists LULD as exit #1; the code correctly omits it per the C2-DROPPED decision.)

### T7c — EXIT_D disabled confirmation

> **Spec says:** `exit_d.enabled=false`; code retained, not evaluated (`EPG_Rapid_Strategy.md §2`).
> **Code does:** `exit_d.enabled=false` in config. In rapid mode EXIT_D is not merely flag-gated — it is **structurally absent** from the worker (no EXIT_D computation at all). `exit_d_enabled` is read at the main level only to forward to `runner._process_event` in parity mode.
> **Match:** ✅ YES
> **Evidence:** Config, `config/strategy.json:29-34` (`"exit_d": { "enabled": false, ... }`). Read for parity pass-through only, `runner_rapid.py:1121-1124,1165-1167`:
> ```python
> exit_d_cfg = phase_cfg.get("exit_d", {})
> exit_d_enabled = exit_d_cfg.get("enabled", False)
> ...
> "exit_d_enabled": exit_d_enabled,
> ```
> No EXIT_D / theta / tau_min evaluation occurs in `_process_event_rapid` (rapid path).

### T7d — LULD module identity

> **Spec says:** EPG-Rapid uses the rebuilt quote-based sticky-reference `core/exits/luld_proximity.py`; but per the resolved decision, LULD is dropped from the rapid stack entirely (used only by the classic runner).
> **Code does:** `runner_rapid.py` imports `detect_luld_halts` from `core.features.luld_halt_detection` (for the Hawkes halt-gap clock), **not** `LuldProximityExit` from `core/exits/luld_proximity.py`. No LULD proximity exit module is imported or used in the rapid path. The rebuilt `luld_proximity` module is reached only indirectly in parity mode (via `runner._process_event`); `luld_proximity_threshold` is computed and plumbed into work items solely for that parity pass-through.
> **Match:** ✅ YES (matches resolved spec — LULD not in rapid stack)
> **Evidence:** Import, `runner_rapid.py:76`:
> ```python
> from core.features.luld_halt_detection import detect_luld_halts
> ```
> No `from core.exits.luld_proximity import ...` exists in `runner_rapid.py`. Parity-only threshold plumbing, `runner_rapid.py:1110-1115,1169`.

---

# T8 — ROC Gate Disabled Confirmation

Files read: `backtest/runner_rapid.py`.

### T8a — Config value

> **Spec says:** ROC disabled in R0/R1 (`roc_min=None`) (`EPG_Rapid_Test_Phases.md R0 context: "ROC gate disabled (roc_min=None)"`).
> **Code does:** `--roc-min` defaults to `None`; `config/strategy.json` has no ROC key; the work item sets `roc_min = args.roc_min` (None unless explicitly passed).
> **Match:** ✅ YES (disabled by default)
> **Evidence:** Arg default, `runner_rapid.py:972-973`:
> ```python
> parser.add_argument("--roc-min", type=float, default=None,
>                     help="Minimum 5-min ROC to enter (None = disabled)")
> ```
> Work item, `runner_rapid.py:1161` (`"roc_min": args.roc_min`). No `roc`/`roc_min` key exists in `config/strategy.json`.

### T8b — Disable branch

> **Spec says:** When ROC is disabled the runner must skip the ROC check entirely (no `roc >= None` evaluation).
> **Code does:** `roc_min` is read once into a local (`runner_rapid.py:420`) and **never referenced again** anywhere in `_process_event_rapid`. There is no ROC computation and no ROC comparison in the entry path — so there is no risk of a `roc_5m >= None` TypeError. ROC is structurally absent (the read at line 420 is effectively dead in rapid mode).
> **Match:** ✅ YES (ROC check skipped entirely; no comparison performed)
> **Evidence:** Sole reference in the worker, `runner_rapid.py:420`:
> ```python
> roc_min = args.get("roc_min", None)
> ```
> A grep of `runner_rapid.py` for `roc` shows no further use inside `_process_event_rapid` (next occurrences are the CLI arg at 972 and main-level plumbing at 1161/1255). The entry dispatch (lines 604-647) contains no ROC term.

### T8c — `RocBuffer` instantiation

> **Spec says:** RocBuffer is the C4 module for ROC; must not affect the entry path when ROC disabled.
> **Code does:** `RocBuffer` is **not imported and not instantiated** anywhere in `runner_rapid.py`. It cannot affect the entry path.
> **Match:** ✅ YES
> **Evidence:** No `RocBuffer` / `roc_buffer` import or construction appears in `runner_rapid.py` (imports block, lines 62-76, and full file). ROC was never wired into the rapid runner.

---

## Cross-area conclusions

1. **Headline (T4e / T3d):** `max_entry_lag_sec` is implemented as an unconditional loop `break`
   placed before the entry/exit dispatch (`runner_rapid.py:590-594`). When set (R1+ = 300s), it
   terminates exit monitoring of an already-open position, force-booking `session_end` at
   end-of-data. This is the mechanism behind "4/5 exits are session_end despite PASS→FAIL
   transitions," and it largely decouples the R1 gate-threshold sweep from the `p_close` exit
   timing it is meant to tune. R0 (`max_entry_lag_sec=null`) is unaffected.

2. **Gate halt-blindness (T6b):** the halt-gap `dt=0` substitution is applied to the Hawkes EMA
   only; the gate's `λ_V` decays normally across halts, contrary to spec §6. Impact is bounded to
   events with detected halt windows (gap ≥300s).

3. **Config flags ignored (T4d):** `epg_window_close_exit.enabled` is never read; the EPG-close
   exit is hard-coded. Behavior is correct but not configurable.

4. **Realism caveat (T1e):** scanner hit uses the exact crossing tick, not a 15–30s poll-grid
   timestamp — optimistic entry timing vs. the live scanner.

5. **Threshold split (T6c):** halt detection uses a 300s gap floor (default), the dt substitution
   uses 60s; the 60s value matches the C3 spec, the 300s value is unreconciled (non-binding).
