"""Historical context fetch + Hawkes/EPG warmup before live feed begins."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
import numpy as np

# Import from backtest.runner first — its module-level sys.path.insert adds
# /app/backtest to sys.path, enabling the bare 'core.*' imports below.
from backtest.runner import _hawkes_replay_with_refit, session_bucket

from core.epg.anchor import EventAnchor
from core.epg.gate import GateState, ParticipationGate
from core.hawkes.engine import HawkesEngine, HawkesState
from core.hawkes.forgetting import HawkesParams

from live.config import CFG
from live.db.pool import get_pool
from backtest.setup_filter import SetupFilterResult, run_setup_filter

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_NS_PER_SEC = 1_000_000_000

_TRADES_URL = "https://api.polygon.io/v3/trades/{ticker}"
_BARS_URL = "https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/{date}/{date}"


@dataclass
class ContextFetchResult:
    engine: HawkesEngine
    anchor: EventAnchor
    gate: ParticipationGate
    prev_gate_state: GateState
    last_ts_ns: int                      # dedup boundary
    lambda_ref_global: float
    lambda_ref_fitted: Optional[float]
    mu_buy_fitted: Optional[float]
    mu_sell_fitted: Optional[float]
    fitted_params: Optional[HawkesParams]
    cold_start_n: int
    degraded_mode: bool
    fetch_ms: int
    tick_timestamps_ns: np.ndarray       # all historical ticks (setup filter buffer init)
    tick_prices: np.ndarray
    tick_sizes: np.ndarray
    session_start_ns: int
    session_end_ns: int
    setup_filter_result: Optional[SetupFilterResult]
    intraday_pct: float                  # scanner pct_change / 100


def _session_bounds_ns(session_date: date) -> tuple[int, int]:
    start = datetime(session_date.year, session_date.month, session_date.day,
                     CFG.context_fetch.session_start_et_hour, 0, 0, tzinfo=_ET)
    end = datetime(session_date.year, session_date.month, session_date.day, 20, 0, 0, tzinfo=_ET)
    return int(start.timestamp() * _NS_PER_SEC), int(end.timestamp() * _NS_PER_SEC)


async def _fetch_trades(
    http: aiohttp.ClientSession,
    ticker: str,
    session_start_ns: int,
    api_key: str,
) -> list[dict]:
    url = _TRADES_URL.format(ticker=ticker)
    params = {
        "timestamp.gte": str(session_start_ns),
        "limit": 50000,
        "apiKey": api_key,
    }
    results = []
    while True:
        async with http.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json()
        results.extend(data.get("results", []))
        next_url = data.get("next_url")
        if not next_url or len(results) >= 100_000:
            break
        url = next_url + f"&apiKey={api_key}"
        params = {}
    return results


async def _fetch_bars(
    http: aiohttp.ClientSession,
    ticker: str,
    session_date: date,
    api_key: str,
) -> list[dict]:
    date_str = session_date.isoformat()
    url = _BARS_URL.format(ticker=ticker, date=date_str)
    params = {"adjusted": "false", "apiKey": api_key}
    async with http.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return data.get("results", [])


def _classify_sides(prices: np.ndarray) -> np.ndarray:
    """Tick-direction classification: price up → BUY (0), down → SELL (1), flat → carry."""
    sides = np.zeros(len(prices), dtype=np.int32)
    for i in range(1, len(prices)):
        if prices[i] > prices[i - 1]:
            sides[i] = 0
        elif prices[i] < prices[i - 1]:
            sides[i] = 1
        else:
            sides[i] = sides[i - 1]
    return sides


async def fetch_context(
    ticker: str,
    session_date: date,
    scanner_context: dict,
    polygon_api_key: str,
    account_equity: float = 0.0,
    theoretical_equity: float = 0.0,
) -> ContextFetchResult:
    """Fetch historical context, warm Hawkes engine and EPG gate."""
    t0 = time.monotonic()
    session_start_ns, session_end_ns = _session_bounds_ns(session_date)
    lambda_ref_global = CFG.hawkes.mu_buy + CFG.hawkes.mu_sell
    intraday_pct = scanner_context.get("pct_change", 0.0) / 100.0

    degraded_mode = False
    fitted_params: Optional[HawkesParams] = None

    try:
        async with aiohttp.ClientSession() as http:
            trades_raw, bars_raw = await asyncio.wait_for(
                asyncio.gather(
                    _fetch_trades(http, ticker, session_start_ns, polygon_api_key),
                    _fetch_bars(http, ticker, session_date, polygon_api_key),
                ),
                timeout=CFG.context_fetch.timeout_s,
            )
    except asyncio.TimeoutError:
        log.warning("%s: context fetch timeout — using global lambda_ref fallback", ticker)
        degraded_mode = True
        trades_raw, bars_raw = [], []

    # Parse and sort trades
    trades_sorted = sorted(trades_raw, key=lambda t: t["sip_timestamp"])
    N = len(trades_sorted)
    log.info("%s: %d historical trades fetched", ticker, N)

    if N < CFG.context_fetch.degraded_min_trades:
        log.warning("%s: only %d trades — DEGRADED mode", ticker, N)
        degraded_mode = True
    elif N < CFG.context_fetch.full_replay_min_trades:
        log.warning("%s: %d trades < %d — using global lambda_ref fallback, DEGRADED",
                    ticker, N, CFG.context_fetch.full_replay_min_trades)
        degraded_mode = True

    if N == 0:
        # No data — return zero-state engine
        return _zero_state_result(
            ticker, session_start_ns, session_end_ns, lambda_ref_global, intraday_pct,
            int((time.monotonic() - t0) * 1000),
        )

    ts_ns = np.array([t["sip_timestamp"] for t in trades_sorted], dtype=np.int64)
    prices = np.array([t["price"] for t in trades_sorted], dtype=np.float64)
    sizes = np.array([t.get("size", 0) for t in trades_sorted], dtype=np.int64)
    sides = _classify_sides(prices)
    t_sec = (ts_ns - session_start_ns) / _NS_PER_SEC

    init_params = {
        "alpha_buy_self": CFG.hawkes.alpha_buy_self,
        "alpha_sell_self": CFG.hawkes.alpha_sell_self,
        "mu_buy": CFG.hawkes.mu_buy,
        "mu_sell": CFG.hawkes.mu_sell,
        "beta": CFG.hawkes.beta,
    }

    lam_buy_out = np.zeros(N, dtype=np.float64)
    lam_sell_out = np.zeros(N, dtype=np.float64)
    E_out = np.zeros(N, dtype=np.float64)
    Edot_out = np.zeros(N, dtype=np.float64)
    n_base_out = np.zeros(N, dtype=np.float64)

    # NOTE: Lee-Ready for historical ticks uses tick direction only (price change sign)
    # because the Polygon trades REST endpoint does not return bid/ask. True Lee-Ready
    # (last-quote comparison) is only possible for live WebSocket ticks.
    fitted_params = _hawkes_replay_with_refit(
        t_sec, sides.astype(np.int32), CFG.hawkes.rho, lambda_ref_global, init_params,
        CFG.hawkes.rho_e, lam_buy_out, lam_sell_out, E_out, Edot_out, n_base_out,
    )

    if fitted_params is not None:
        lambda_ref_fitted = fitted_params.mu_buy + fitted_params.mu_sell
        mu_buy_fitted = fitted_params.mu_buy
        mu_sell_fitted = fitted_params.mu_sell
        params_for_engine = fitted_params
    else:
        lambda_ref_fitted = None
        mu_buy_fitted = None
        mu_sell_fitted = None
        # Build a minimal HawkesParams-like from config for engine init
        class _ConfigParams:
            alpha_buy_self = CFG.hawkes.alpha_buy_self
            alpha_sell_self = CFG.hawkes.alpha_sell_self
            mu_buy = CFG.hawkes.mu_buy
            mu_sell = CFG.hawkes.mu_sell
            beta = CFG.hawkes.beta
        params_for_engine = _ConfigParams()

    # Init HawkesEngine with fitted (or config) params
    lambda_ref_for_engine = lambda_ref_fitted or lambda_ref_global
    engine = HawkesEngine(
        beta_mle=params_for_engine.beta,
        alpha_self_buy=params_for_engine.alpha_buy_self,
        alpha_cross_buy=0.0,
        mu_buy=params_for_engine.mu_buy,
        mu_sell=params_for_engine.mu_sell,
        lambda_ref=lambda_ref_for_engine,
        alpha_self_sell=params_for_engine.alpha_sell_self,
        alpha_cross_sell=0.0,
    )

    # Tail-replay last N seconds through engine to warm R state
    tail_cutoff = t_sec[-1] - CFG.context_fetch.tail_replay_sec
    tail_mask = t_sec >= tail_cutoff
    tail_t = t_sec[tail_mask]
    tail_sides = sides[tail_mask]
    for i in range(len(tail_t)):
        engine.update(float(tail_t[i]), int(tail_sides[i]))

    # Replay EventAnchor + ParticipationGate through all historical ticks
    anchor = EventAnchor(
        lambda_ref=lambda_ref_for_engine,
        k_multiplier=CFG.epg.t_event_threshold,
    )
    gate = ParticipationGate(
        half_life_seconds=CFG.epg.window_close_sec,
        peak_threshold_p=CFG.epg.lambda_v_threshold,
    )

    prev_gate_state = GateState.INACTIVE
    for i in range(N):
        lambda_hat_i = lam_buy_out[i] + lam_sell_out[i]
        t_ev = anchor.update(lambda_hat_i, float(t_sec[i]))
        if t_ev is not None and gate.t_event is None:
            gate.activate(t_ev)
        if gate.t_event is not None:
            dollar_vol = float(prices[i]) * float(sizes[i])
            prev_gate_state = gate.update(dollar_vol, float(t_sec[i]))

    # Run setup filter on full historical tick buffer
    sf_result: Optional[SetupFilterResult] = None
    try:
        sf_result = run_setup_filter(
            timestamps=ts_ns,
            prices=prices,
            sizes=sizes,
            session_start_ns=session_start_ns,
            session_end_ns=session_end_ns,
        )
    except Exception:
        log.exception("%s: setup filter failed on historical data", ticker)

    fetch_ms = int((time.monotonic() - t0) * 1000)
    log.info("%s: context fetch complete in %dms, N=%d, degraded=%s",
             ticker, fetch_ms, N, degraded_mode)

    alpha_buy_fitted = fitted_params.alpha_buy_self if fitted_params is not None else None
    alpha_sell_fitted = fitted_params.alpha_sell_self if fitted_params is not None else None
    n_base_at_cold_start = float(n_base_out[-1]) if N > 0 else None
    setup_filter_passes = sf_result.passes if sf_result is not None else None
    _raw_score = getattr(sf_result, "q_tilde", None) if sf_result is not None else None
    try:
        setup_filter_score = float(_raw_score) if _raw_score is not None else None
    except (TypeError, ValueError):
        setup_filter_score = None

    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO sessions
                    (strategy_id, ticker, session_date, scanner_fire_ns,
                     scanner_rank, scanner_n, scanner_heat, scanner_quartile,
                     multi_day_runner, context_fetch_ms, cold_start_n, degraded_mode,
                     lambda_ref_global, lambda_ref_fitted,
                     mu_buy_fitted, mu_sell_fitted,
                     alpha_buy_fitted, alpha_sell_fitted,
                     n_base_at_cold_start,
                     setup_filter_score, setup_filter_passes,
                     account_equity_start, theoretical_equity_start)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23)
                ON CONFLICT (strategy_id, ticker, session_date) DO UPDATE
                    SET context_fetch_ms = EXCLUDED.context_fetch_ms,
                        cold_start_n = EXCLUDED.cold_start_n,
                        degraded_mode = EXCLUDED.degraded_mode
                """,
                CFG.strategy_id, ticker, session_date,
                scanner_context.get("snapshot_ns"),
                scanner_context.get("scanner_rank"),
                scanner_context.get("scanner_n"),
                scanner_context.get("scanner_heat"),
                scanner_context.get("scanner_quartile"),
                False,  # multi_day_runner: Phase H — collect only
                fetch_ms, N, degraded_mode,
                lambda_ref_global, lambda_ref_fitted,
                mu_buy_fitted, mu_sell_fitted,
                alpha_buy_fitted, alpha_sell_fitted,
                n_base_at_cold_start,
                setup_filter_score, setup_filter_passes,
                account_equity if account_equity > 0 else None,
                theoretical_equity if theoretical_equity > 0 else None,
            )
    except Exception:
        log.exception("%s: failed to write session record", ticker)

    return ContextFetchResult(
        engine=engine,
        anchor=anchor,
        gate=gate,
        prev_gate_state=prev_gate_state,
        last_ts_ns=int(ts_ns[-1]) if N > 0 else 0,
        lambda_ref_global=lambda_ref_global,
        lambda_ref_fitted=lambda_ref_fitted,
        mu_buy_fitted=mu_buy_fitted,
        mu_sell_fitted=mu_sell_fitted,
        fitted_params=fitted_params,
        cold_start_n=N,
        degraded_mode=degraded_mode,
        fetch_ms=fetch_ms,
        tick_timestamps_ns=ts_ns,
        tick_prices=prices,
        tick_sizes=sizes,
        session_start_ns=session_start_ns,
        session_end_ns=session_end_ns,
        setup_filter_result=sf_result,
        intraday_pct=intraday_pct,
    )


def _zero_state_result(
    ticker: str,
    session_start_ns: int,
    session_end_ns: int,
    lambda_ref_global: float,
    intraday_pct: float,
    fetch_ms: int,
) -> ContextFetchResult:
    engine = HawkesEngine(
        beta_mle=CFG.hawkes.beta,
        alpha_self_buy=CFG.hawkes.alpha_buy_self,
        alpha_cross_buy=0.0,
        mu_buy=CFG.hawkes.mu_buy,
        mu_sell=CFG.hawkes.mu_sell,
        lambda_ref=lambda_ref_global,
        alpha_self_sell=CFG.hawkes.alpha_sell_self,
        alpha_cross_sell=0.0,
    )
    anchor = EventAnchor(lambda_ref=lambda_ref_global, k_multiplier=CFG.epg.t_event_threshold)
    gate = ParticipationGate(
        half_life_seconds=CFG.epg.window_close_sec,
        peak_threshold_p=CFG.epg.lambda_v_threshold,
    )
    return ContextFetchResult(
        engine=engine,
        anchor=anchor,
        gate=gate,
        prev_gate_state=GateState.INACTIVE,
        last_ts_ns=0,
        lambda_ref_global=lambda_ref_global,
        lambda_ref_fitted=None,
        mu_buy_fitted=None,
        mu_sell_fitted=None,
        fitted_params=None,
        cold_start_n=0,
        degraded_mode=True,
        fetch_ms=fetch_ms,
        tick_timestamps_ns=np.array([], dtype=np.int64),
        tick_prices=np.array([], dtype=np.float64),
        tick_sizes=np.array([], dtype=np.int64),
        session_start_ns=session_start_ns,
        session_end_ns=session_end_ns,
        setup_filter_result=None,
        intraday_pct=intraday_pct,
    )
