"""Pure EXIT_D simulator — given a replay and Phase S trades, compute
where (and whether) EXIT_D would have fired per trade.

Recovery params (theta_low, tau_recovery) are deferred to a later phase
that wires re-entry block into the runner; this simulator only models
the per-trade exit decision.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from tools.exit_d_tuning.replay import EventReplay


_NS_PER_SEC = 1_000_000_000


@dataclass
class ExitDSimulation:
    trade_seq: np.ndarray
    entry_idx: np.ndarray
    entry_ts: np.ndarray
    entry_price: np.ndarray
    original_exit_idx: np.ndarray
    original_exit_ts: np.ndarray
    original_exit_price: np.ndarray
    original_pnl_pct: np.ndarray

    exit_d_would_fire: np.ndarray
    exit_d_idx: np.ndarray
    exit_d_ts: np.ndarray
    exit_d_price: np.ndarray
    exit_d_pnl_pct: np.ndarray
    exit_d_reason: np.ndarray  # "exit_d_fired" | "original_exit_first" | "disabled_high_i_at_entry"


def _empty(n: int = 0) -> ExitDSimulation:
    return ExitDSimulation(
        trade_seq=np.array([], dtype=np.int64),
        entry_idx=np.array([], dtype=np.int64),
        entry_ts=np.array([], dtype=np.int64),
        entry_price=np.array([], dtype=np.float64),
        original_exit_idx=np.array([], dtype=np.int64),
        original_exit_ts=np.array([], dtype=np.int64),
        original_exit_price=np.array([], dtype=np.float64),
        original_pnl_pct=np.array([], dtype=np.float64),
        exit_d_would_fire=np.array([], dtype=bool),
        exit_d_idx=np.array([], dtype=np.int64),
        exit_d_ts=np.array([], dtype=np.int64),
        exit_d_price=np.array([], dtype=np.float64),
        exit_d_pnl_pct=np.array([], dtype=np.float64),
        exit_d_reason=np.array([], dtype=object),
    )


def simulate_exit_d(
    replay: EventReplay,
    phase_s_trades: pd.DataFrame,
    theta: float,
    tau_min_sec: float,
) -> ExitDSimulation:
    """For each Phase S trade: where would EXIT_D have fired?

    Logic per the Phase T spec:
      - If I(entry) > theta, EXIT_D is disabled for this trade.
      - Walk forward: when I(t) > theta, start (or continue) a timer.
        When I(t) <= theta, reset timer to 0.
        When the timer reaches tau_min_sec, fire EXIT_D at the next tick.
      - Whichever fires first (EXIT_D or the original exit) wins.

    Pure: same inputs always produce the same output.

    Each Phase S trade is non-overlapping (sequential by design), so the
    per-trade walk is bounded — total work is O(N_replay) across all trades.
    """
    n_trades = len(phase_s_trades)
    if n_trades == 0:
        return _empty()

    ts_ns = replay.timestamps_ns
    prices = replay.prices
    intensity = replay.intensity_ratio
    N = len(ts_ns)

    trade_seq = phase_s_trades["trade_seq"].to_numpy(dtype=np.int64)
    entry_idx = phase_s_trades["entry_idx"].to_numpy(dtype=np.int64)
    entry_ts = phase_s_trades["entry_ts"].to_numpy(dtype=np.int64)
    entry_price = phase_s_trades["entry_price"].to_numpy(dtype=np.float64)
    original_exit_idx = phase_s_trades["exit_idx"].to_numpy(dtype=np.int64)
    original_exit_ts = phase_s_trades["exit_ts"].to_numpy(dtype=np.int64)
    original_exit_price = phase_s_trades["exit_price"].to_numpy(dtype=np.float64)
    original_pnl_pct = phase_s_trades["pnl_pct"].to_numpy(dtype=np.float64)

    exit_d_would_fire = np.zeros(n_trades, dtype=bool)
    exit_d_idx_out = np.full(n_trades, -1, dtype=np.int64)
    exit_d_ts_out = np.zeros(n_trades, dtype=np.int64)
    exit_d_price_out = np.full(n_trades, np.nan, dtype=np.float64)
    exit_d_pnl_out = np.full(n_trades, np.nan, dtype=np.float64)
    exit_d_reason_out = np.empty(n_trades, dtype=object)

    tau_min_ns = tau_min_sec * _NS_PER_SEC

    for k in range(n_trades):
        ent_i = int(entry_idx[k])
        orig_exit_i = int(original_exit_idx[k])

        # Disabled if I(entry) already > theta
        if 0 <= ent_i < N and not np.isnan(intensity[ent_i]) and intensity[ent_i] > theta:
            exit_d_reason_out[k] = "disabled_high_i_at_entry"
            continue

        timer_start_ts: int | None = None
        fired = False
        fire_at_idx = -1
        # Bound search at the original exit; fill is the next tick
        end_i = min(orig_exit_i, N - 2)
        i = ent_i + 1
        while i <= end_i:
            it = intensity[i]
            if not np.isnan(it) and it > theta:
                if timer_start_ts is None:
                    timer_start_ts = int(ts_ns[i])
                elif int(ts_ns[i]) - timer_start_ts >= tau_min_ns:
                    fired = True
                    fire_at_idx = i
                    break
            else:
                timer_start_ts = None
            i += 1

        if fired and fire_at_idx >= 0:
            fill_idx = min(fire_at_idx + 1, N - 1)
            ep = float(entry_price[k])
            xp = float(prices[fill_idx])
            pnl = (xp - ep) / ep * 100.0 if ep > 0 else 0.0
            exit_d_would_fire[k] = True
            exit_d_idx_out[k] = fill_idx
            exit_d_ts_out[k] = int(ts_ns[fill_idx])
            exit_d_price_out[k] = xp
            exit_d_pnl_out[k] = pnl
            exit_d_reason_out[k] = "exit_d_fired"
        else:
            exit_d_reason_out[k] = "original_exit_first"

    return ExitDSimulation(
        trade_seq=trade_seq,
        entry_idx=entry_idx,
        entry_ts=entry_ts,
        entry_price=entry_price,
        original_exit_idx=original_exit_idx,
        original_exit_ts=original_exit_ts,
        original_exit_price=original_exit_price,
        original_pnl_pct=original_pnl_pct,
        exit_d_would_fire=exit_d_would_fire,
        exit_d_idx=exit_d_idx_out,
        exit_d_ts=exit_d_ts_out,
        exit_d_price=exit_d_price_out,
        exit_d_pnl_pct=exit_d_pnl_out,
        exit_d_reason=exit_d_reason_out,
    )


# ── TB3a smoke check ──────────────────────────────────────────────────


def _smoke_check():
    """Pure-function check: identical inputs → identical outputs."""
    n = 20
    replay = EventReplay(
        timestamps_ns=np.arange(n, dtype=np.int64) * _NS_PER_SEC,
        prices=np.linspace(10.0, 9.0, n, dtype=np.float64),
        sides=np.zeros(n, dtype=np.int8),
        lambda_buy=np.full(n, 0.5, dtype=np.float64),
        lambda_sell=np.full(n, 0.5, dtype=np.float64),
        intensity_ratio=np.linspace(0.4, 0.9, n, dtype=np.float64),
        epg_state=np.full(n, 2, dtype=np.int8),
        pass_window_open_ts=np.array([0], dtype=np.int64),
        pass_window_close_ts=np.array([n * _NS_PER_SEC], dtype=np.int64),
        t_event_ns=0,
    )
    df = pd.DataFrame({
        "trade_seq": [0],
        "entry_idx": [2],
        "entry_ts": [int(replay.timestamps_ns[2])],
        "entry_price": [9.9],
        "exit_idx": [18],
        "exit_ts": [int(replay.timestamps_ns[18])],
        "exit_price": [9.05],
        "pnl_pct": [-8.6],
    })
    a = simulate_exit_d(replay, df, theta=0.7, tau_min_sec=2.0)
    b = simulate_exit_d(replay, df, theta=0.7, tau_min_sec=2.0)
    assert np.array_equal(a.exit_d_would_fire, b.exit_d_would_fire)
    assert np.array_equal(a.exit_d_idx, b.exit_d_idx)
    assert np.array_equal(a.exit_d_reason, b.exit_d_reason)
    print("simulate_exit_d smoke check: OK "
          f"(fire={a.exit_d_would_fire[0]}, reason={a.exit_d_reason[0]})")


if __name__ == "__main__":
    _smoke_check()
