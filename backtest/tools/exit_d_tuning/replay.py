"""Tick-level replay of Hawkes engine + EPG with disk caching.

Reuses the cold-start MLE → online refit → EPG activation sequence from
`backtest.runner_screening_only`. Imports the underlying engine helper
rather than re-implementing the Hawkes logic.

Cache format
------------
For an event keyed by (TICKER, DATE), two files are written:

  cache_dir/{TICKER}_{DATE}_replay.parquet   — one row per tick, columns:
      timestamp_ns, price, side, lambda_buy, lambda_sell,
      intensity_ratio, epg_state
  cache_dir/{TICKER}_{DATE}_replay.json      — sidecar:
      pass_window_open_ts, pass_window_close_ts, t_event_ns, ticker, date,
      schema_version
"""
from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from data.schemas.mom_db import CONFIG_DIR
from data.loaders.trades import (
    load_trades, list_events, compute_lambda_ref_per_event,
)
from data.loaders.quotes import load_quotes
from core.ofi.trade_ofi import compute_trade_ofi
from core.epg.anchor import EventAnchor
from core.epg.gate import ParticipationGate, GateState

# Production Hawkes refit (same call path as runner_screening_only.py)
from backtest.runner import _hawkes_replay_with_refit


# ── EPG constants (must match Phase S) ────────────────────────────────

_EPG_K = 5
_EPG_TAU = 300.0
_EPG_P = 0.65
_EPG_WARMUP = 300.0

_STATE_NUM = {
    GateState.INACTIVE: 0,
    GateState.WARMUP: 1,
    GateState.PASS: 2,
    GateState.FAIL: 3,
}

_SCHEMA_VERSION = 1


# ── Result dataclass ──────────────────────────────────────────────────


@dataclass
class EventReplay:
    timestamps_ns: np.ndarray        # int64
    prices: np.ndarray               # float64
    sides: np.ndarray                # int8
    lambda_buy: np.ndarray           # float64
    lambda_sell: np.ndarray          # float64
    intensity_ratio: np.ndarray      # float64, NaN where lam_total == 0
    epg_state: np.ndarray            # int8 (0..3)
    pass_window_open_ts: np.ndarray  # int64 (one per PASS run)
    pass_window_close_ts: np.ndarray # int64 (one per PASS run)
    t_event_ns: Optional[int]


# ── Helpers ───────────────────────────────────────────────────────────


def _resolve_mom_pct(ticker: str, date: str) -> float:
    for ev in list_events(min_mom=0.0, require_date=True):
        if ev["ticker"] == ticker and ev["date"] == date:
            return ev["mom_pct"]
    raise FileNotFoundError(f"No event in catalog for {ticker} {date}")


def _cache_paths(cache_dir: Path, ticker: str, date: str) -> tuple[Path, Path]:
    base = cache_dir / f"{ticker}_{date}_replay"
    return base.with_suffix(".parquet"), base.with_suffix(".json")


def _load_cache(cache_dir: Path, ticker: str, date: str) -> Optional[EventReplay]:
    pq_path, sidecar_path = _cache_paths(cache_dir, ticker, date)
    if not pq_path.exists() or not sidecar_path.exists():
        return None
    try:
        df = pd.read_parquet(pq_path)
        with open(sidecar_path) as f:
            sidecar = json.load(f)
        if sidecar.get("schema_version") != _SCHEMA_VERSION:
            return None
    except Exception:
        return None

    return EventReplay(
        timestamps_ns=df["timestamp_ns"].to_numpy(dtype=np.int64),
        prices=df["price"].to_numpy(dtype=np.float64),
        sides=df["side"].to_numpy(dtype=np.int8),
        lambda_buy=df["lambda_buy"].to_numpy(dtype=np.float64),
        lambda_sell=df["lambda_sell"].to_numpy(dtype=np.float64),
        intensity_ratio=df["intensity_ratio"].to_numpy(dtype=np.float64),
        epg_state=df["epg_state"].to_numpy(dtype=np.int8),
        pass_window_open_ts=np.asarray(sidecar["pass_window_open_ts"],
                                        dtype=np.int64),
        pass_window_close_ts=np.asarray(sidecar["pass_window_close_ts"],
                                         dtype=np.int64),
        t_event_ns=sidecar.get("t_event_ns"),
    )


def _save_cache(replay: EventReplay, cache_dir: Path,
                ticker: str, date: str) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    pq_path, sidecar_path = _cache_paths(cache_dir, ticker, date)
    df = pd.DataFrame({
        "timestamp_ns": replay.timestamps_ns,
        "price": replay.prices,
        "side": replay.sides,
        "lambda_buy": replay.lambda_buy,
        "lambda_sell": replay.lambda_sell,
        "intensity_ratio": replay.intensity_ratio,
        "epg_state": replay.epg_state,
    })
    df.to_parquet(pq_path)
    sidecar = {
        "schema_version": _SCHEMA_VERSION,
        "ticker": ticker,
        "date": date,
        "pass_window_open_ts": replay.pass_window_open_ts.tolist(),
        "pass_window_close_ts": replay.pass_window_close_ts.tolist(),
        "t_event_ns": (int(replay.t_event_ns)
                       if replay.t_event_ns is not None else None),
    }
    with open(sidecar_path, "w") as f:
        json.dump(sidecar, f, indent=2)


# ── Main entry ────────────────────────────────────────────────────────


def replay_event_with_intensity(
    ticker: str,
    date: str,
    mom_folder: Optional[Path] = None,
    cache_dir: Optional[Path] = None,
    force_recompute: bool = False,
) -> EventReplay:
    """Replay one event end-to-end with disk-backed cache.

    `mom_folder` is accepted for API compatibility; the actual path is
    resolved from the event catalog by ticker+date (so the caller does
    not need to know the {MOM} suffix).

    If `cache_dir` is provided and a valid cache file exists, the cached
    replay is returned without re-computing. Set `force_recompute=True`
    to bypass the cache.
    """
    if cache_dir is not None and not force_recompute:
        cached = _load_cache(cache_dir, ticker, date)
        if cached is not None:
            return cached

    mom_pct = _resolve_mom_pct(ticker, date)

    # ── Load data ──
    td = load_trades(ticker, date, mom_pct)
    qd = load_quotes(ticker, date, mom_pct)
    if td.n_trades < 30 or qd.n_quotes < 10:
        raise ValueError(
            f"insufficient data for {ticker} {date}: "
            f"{td.n_trades} trades, {qd.n_quotes} quotes"
        )

    # ── Hawkes config ──
    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_median = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    fp = hawkes_median
    phase_a_path = (
        Path(__file__).resolve().parents[2]
        / "results" / "phase_a" / "production_fit_results.json"
    )
    if phase_a_path.exists():
        with open(phase_a_path) as f:
            phase_a_results = json.load(f)
        for r in phase_a_results:
            if (r.get("status") == "success" and r.get("ticker") == ticker
                    and r.get("date") == date and "final_params" in r):
                fp = r["final_params"]
                break

    rho = hawkes_median.get("rho", 0.99)
    rho_E = rho

    # ── Lee-Ready sides ──
    tier_qbar = q_bar_cfg.get("wide", {}).get("median", 250.0)
    ofi_result = compute_trade_ofi(
        trade_timestamps=td.timestamps,
        trade_prices=td.prices,
        trade_sizes=td.sizes.astype(np.float64),
        quote_timestamps=qd.timestamps,
        quote_bid_prices=qd.bid_prices,
        quote_ask_prices=qd.ask_prices,
        quote_bid_sizes=qd.bid_sizes.astype(np.float64),
        quote_ask_sizes=qd.ask_sizes.astype(np.float64),
        window_sec=10.0,
        q_bar_fallback=tier_qbar,
    )
    sides = ofi_result.sides

    # ── Hawkes replay with online refit ──
    N = td.n_trades
    lam_buy_out = np.zeros(N, dtype=np.float64)
    lam_sell_out = np.zeros(N, dtype=np.float64)
    E_out = np.zeros(N, dtype=np.float64)
    Edot_out = np.zeros(N, dtype=np.float64)
    n_base_out = np.zeros(N, dtype=np.float64)

    global_lambda_ref = fp["mu_buy"] + fp["mu_sell"]
    per_event_lref = compute_lambda_ref_per_event(ticker, date)
    lambda_ref = (per_event_lref
                  if (not math.isnan(per_event_lref) and per_event_lref > 0)
                  else global_lambda_ref)

    cold_start_params = _hawkes_replay_with_refit(
        t_sec=td.t_sec, sides=sides,
        rho=rho, lambda_ref=lambda_ref,
        init_params=fp, rho_E=rho_E,
        lam_buy_out=lam_buy_out, lam_sell_out=lam_sell_out,
        E_out=E_out, Edot_out=Edot_out, n_base_out=n_base_out,
    )
    lambda_hat = lam_buy_out + lam_sell_out

    # ── EPG ──
    global_lref_epg = fp["mu_buy"] + fp["mu_sell"]
    anchor = EventAnchor(lambda_ref=global_lref_epg, k_multiplier=_EPG_K)
    if cold_start_params is not None:
        lref_epg = cold_start_params.mu_buy + cold_start_params.mu_sell
        if lref_epg > 0:
            anchor.set_lambda_ref(lref_epg)
    gate = ParticipationGate(
        half_life_seconds=_EPG_TAU,
        peak_threshold_p=_EPG_P,
        warmup_seconds=_EPG_WARMUP,
    )

    epg_state = np.zeros(N, dtype=np.int8)
    pass_open_ts: list[int] = []
    pass_close_ts: list[int] = []
    pass_open_cur: Optional[int] = None
    t_event_ns: Optional[int] = None
    t_event_fired = False

    for i in range(N):
        t_ev = anchor.update(lambda_hat[i], td.t_sec[i])
        if t_ev is not None and not t_event_fired:
            gate.activate(t_ev)
            t_event_fired = True
            t_event_ns = int(td.timestamps[i])

        dv = float(td.prices[i]) * float(td.sizes[i])
        st = gate.update(dv, td.t_sec[i])
        epg_state[i] = _STATE_NUM[st]

        ts_i = int(td.timestamps[i])
        if st == GateState.PASS and pass_open_cur is None:
            pass_open_cur = ts_i
        elif st != GateState.PASS and pass_open_cur is not None:
            pass_open_ts.append(pass_open_cur)
            pass_close_ts.append(ts_i)
            pass_open_cur = None

    if pass_open_cur is not None:
        pass_open_ts.append(pass_open_cur)
        pass_close_ts.append(int(td.timestamps[N - 1]))

    # ── Intensity ratio ──
    lam_total = lam_buy_out + lam_sell_out
    with np.errstate(invalid="ignore", divide="ignore"):
        intensity_ratio = np.where(
            lam_total > 0, lam_sell_out / lam_total, np.nan
        )

    # ── Length sanity assertion ──
    n_arrays = {
        "timestamps": len(td.timestamps), "prices": len(td.prices),
        "sides": len(sides), "lambda_buy": len(lam_buy_out),
        "lambda_sell": len(lam_sell_out),
        "intensity_ratio": len(intensity_ratio), "epg_state": len(epg_state),
    }
    if len(set(n_arrays.values())) != 1:
        raise AssertionError(f"Length mismatch in EventReplay arrays: {n_arrays}")

    replay = EventReplay(
        timestamps_ns=np.asarray(td.timestamps, dtype=np.int64),
        prices=np.asarray(td.prices, dtype=np.float64),
        sides=np.asarray(sides, dtype=np.int8),
        lambda_buy=lam_buy_out,
        lambda_sell=lam_sell_out,
        intensity_ratio=intensity_ratio,
        epg_state=epg_state,
        pass_window_open_ts=np.asarray(pass_open_ts, dtype=np.int64),
        pass_window_close_ts=np.asarray(pass_close_ts, dtype=np.int64),
        t_event_ns=t_event_ns,
    )

    if cache_dir is not None:
        _save_cache(replay, cache_dir, ticker, date)

    return replay
