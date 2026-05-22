"""LiveSignalState: per-ticker signal computation wrapping backtest components."""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import numpy as np

# backtest.runner import seeds sys.path with /app/backtest so bare 'core.*' works
from backtest.runner import session_bucket

from core.epg.anchor import EventAnchor
from core.epg.gate import GateState, ParticipationGate
from core.exits.luld_proximity import LuldProximityExit, ProximityState
from core.hawkes.engine import HawkesEngine, HawkesState
from core.hawkes.forgetting import HawkesParams, fit_online

from live.config import CFG
from live.orders.risk import OrderRequest
from live.signals.context_fetch import ContextFetchResult
from backtest.setup_filter import SetupFilterResult, run_setup_filter

log = logging.getLogger(__name__)

_NS_PER_SEC = 1_000_000_000
_REFIT_WINDOW = 10000


@dataclass
class SignalResult:
    sip_timestamp: int
    lambda_buy: float
    lambda_sell: float
    lambda_hat: float
    gate_state: str
    q_tilde: Optional[float]
    order_signal: Optional[str]   # 'ENTRY', 'EXIT_D', 'LULD', 'EPG_CLOSE', or None
    is_trade: bool
    side: Optional[int] = None                          # Lee-Ready: 0=BUY, 1=SELL (trades only)
    signal_events: list = field(default_factory=list)   # per-transition only; tuples for batch writer
    hawkes_refit_record: Optional[tuple] = None         # set when a refit fires


class LiveSignalState:
    """Per-ticker signal state machine.

    Wraps HawkesEngine, EventAnchor, ParticipationGate, LuldProximityExit,
    and the rolling setup filter. Returns SignalResult on each tick.
    Never touches DB or broker directly.
    """

    def __init__(
        self,
        ticker: str,
        ctx: ContextFetchResult,
        scanner_context: dict,
        session_date: date,
    ) -> None:
        self._ticker = ticker
        self._engine: HawkesEngine = ctx.engine
        self._anchor: EventAnchor = ctx.anchor
        self._gate: ParticipationGate = ctx.gate
        self._luld = LuldProximityExit()
        self._session_date = session_date

        self._session_start_ns: int = ctx.session_start_ns
        self._session_end_ns: int = ctx.session_end_ns
        self._last_ts_ns: int = ctx.last_ts_ns

        # Setup filter buffer — pre-populated with historical ticks
        self._sf_ts: list[int] = list(ctx.tick_timestamps_ns)
        self._sf_prices: list[float] = list(ctx.tick_prices.astype(float))
        self._sf_sizes: list[int] = list(ctx.tick_sizes.astype(int))
        self._last_bar_minute: int = self._sf_ts[-1] // (_NS_PER_SEC * 60) if self._sf_ts else -1

        sf = ctx.setup_filter_result
        self._current_q_tilde: Optional[float] = (
            float(sf.q_tilde[-1]) if sf and len(sf.q_tilde) > 0 else None
        )
        self._current_sf_passes: bool = sf.passes if sf else False

        # Hawkes refit tracking
        self._lambda_ref: float = ctx.lambda_ref_fitted or ctx.lambda_ref_global
        self._fitted_params: Optional[HawkesParams] = ctx.fitted_params
        self._refit_buffer: deque = deque(maxlen=_REFIT_WINDOW)
        self._trade_count: int = 0
        self._refit_count: int = 0

        # EPG state tracking
        self._prev_gate_state: GateState = ctx.prev_gate_state

        # EXIT_D state
        self._exit_d_timer_start: Optional[float] = None
        self._i_entry: Optional[float] = None  # I(t) at entry — if > theta, EXIT_D disabled

        # Position tracking
        self._in_position: bool = False
        self._session_bucket_at_entry: Optional[str] = None

        # Last known quote for Lee-Ready and LULD
        self._last_bid: Optional[float] = None
        self._last_ask: Optional[float] = None

        # Scanner context
        self._intraday_pct: float = scanner_context.get("pct_change", 0.0) / 100.0
        self._scanner_context: dict = scanner_context

        # LULD freeze state
        self._luld_frozen: bool = False

        # Last Hawkes state — for i_entry recording at order fill
        self._last_imbalance: float = 0.5
        self._last_lambda_hat: float = 0.0

    def freeze(self) -> None:
        """Freeze signal processing during LULD halt."""
        self._luld_frozen = True
        self._engine.freeze()
        log.info("%s: signal state frozen (LULD halt)", self._ticker)

    def resume(self, halt_duration_sec: float = 0.0) -> None:
        """Resume after LULD halt."""
        self._luld_frozen = False
        self._engine.resume(halt_duration_sec)
        log.info("%s: signal state resumed", self._ticker)

    def record_entry(self, session_bkt: str, i_entry: float) -> None:
        """Called by order_worker after entry fill confirms."""
        self._in_position = True
        self._session_bucket_at_entry = session_bkt
        self._i_entry = i_entry
        self._exit_d_timer_start = None

    def record_exit(self) -> None:
        """Called by order_worker after exit fill confirms."""
        self._in_position = False
        self._session_bucket_at_entry = None
        self._i_entry = None
        self._exit_d_timer_start = None

    def update_quote(self, quote: dict) -> SignalResult:
        """Process a quote message. Updates last bid/ask."""
        self._last_bid = quote.get("bp")
        self._last_ask = quote.get("ap")
        ts_ns = quote.get("t", 0)
        return SignalResult(
            sip_timestamp=ts_ns,
            lambda_buy=0.0, lambda_sell=0.0, lambda_hat=0.0,
            gate_state=self._prev_gate_state.value,
            q_tilde=self._current_q_tilde,
            order_signal=None,
            is_trade=False,
        )

    def update_trade(self, tick: dict) -> SignalResult:
        """Process a trade tick. Returns SignalResult possibly containing an order signal."""
        ts_ns: int = tick.get("t", 0)

        # Dedup at live handoff boundary
        if ts_ns <= self._last_ts_ns:
            return SignalResult(
                sip_timestamp=ts_ns,
                lambda_buy=0.0, lambda_sell=0.0, lambda_hat=0.0,
                gate_state=self._prev_gate_state.value,
                q_tilde=self._current_q_tilde,
                order_signal=None,
                is_trade=True,
            )
        self._last_ts_ns = ts_ns

        if self._luld_frozen:
            return SignalResult(
                sip_timestamp=ts_ns,
                lambda_buy=0.0, lambda_sell=0.0, lambda_hat=0.0,
                gate_state=self._prev_gate_state.value,
                q_tilde=self._current_q_tilde,
                order_signal=None,
                is_trade=True,
            )

        price: float = tick.get("p", 0.0)
        size: int = tick.get("s", 0)
        t_sec = (ts_ns - self._session_start_ns) / _NS_PER_SEC
        events: list = []

        # Lee-Ready classification
        side = self._lee_ready(price)

        # Hawkes update
        hawkes_state: HawkesState = self._engine.update(t_sec, side)

        # EventAnchor
        t_ev = self._anchor.update(hawkes_state.lambda_hat, t_sec)
        if t_ev is not None and self._gate.t_event is None:
            self._gate.activate(t_ev)
            events.append((
                CFG.strategy_id, self._ticker, self._session_date,
                ts_ns, "T_EVENT_FIRE",
                hawkes_state.lambda_hat, self._lambda_ref,
                self._prev_gate_state.value, self._prev_gate_state.value, None,
            ))

        # ParticipationGate
        prev_gate_state = self._prev_gate_state
        dollar_vol = price * size
        gate_state: GateState = (
            self._gate.update(dollar_vol, t_sec)
            if self._gate.t_event is not None
            else GateState.INACTIVE
        )

        # Detect EPG state transitions
        if prev_gate_state != GateState.PASS and gate_state == GateState.PASS:
            events.append((
                CFG.strategy_id, self._ticker, self._session_date,
                ts_ns, "EPG_PASS_OPEN",
                hawkes_state.lambda_hat, self._lambda_ref,
                prev_gate_state.value, gate_state.value, None,
            ))
        elif prev_gate_state == GateState.PASS and gate_state != GateState.PASS:
            events.append((
                CFG.strategy_id, self._ticker, self._session_date,
                ts_ns, "EPG_PASS_CLOSE",
                hawkes_state.lambda_hat, self._lambda_ref,
                prev_gate_state.value, gate_state.value, None,
            ))

        # Setup filter: update buffer, re-run on 1-minute boundary
        bar_minute = ts_ns // (_NS_PER_SEC * 60)
        self._sf_ts.append(ts_ns)
        self._sf_prices.append(price)
        self._sf_sizes.append(size)
        if bar_minute != self._last_bar_minute:
            self._last_bar_minute = bar_minute
            self._recompute_setup_filter()

        # Track imbalance and intensity for bot readout
        lam_total = hawkes_state.lambda_buy + hawkes_state.lambda_sell
        if lam_total > 1e-10:
            self._last_imbalance = hawkes_state.lambda_sell / lam_total
        self._last_lambda_hat = hawkes_state.lambda_hat

        # LULD proximity
        luld_result = self._luld.update(ts_ns, price, self._last_bid, self._last_ask)

        # Online Hawkes refit
        self._refit_buffer.append((t_sec, side))
        self._trade_count += 1
        refit_record = None
        if self._trade_count % CFG.hawkes.refit_interval_trades == 0:
            refit_record = self._maybe_refit(ts_ns)

        # Determine order signal — track EXIT_D timer start for transition logging
        prev_exit_d_timer = self._exit_d_timer_start
        order_signal = self._evaluate_signals(
            hawkes_state, gate_state, luld_result, price, t_sec, ts_ns
        )

        # Detect EXIT_D timer start
        if self._exit_d_timer_start is not None and prev_exit_d_timer is None:
            events.append((
                CFG.strategy_id, self._ticker, self._session_date,
                ts_ns, "EXIT_D_TIMER_START",
                hawkes_state.lambda_hat, self._lambda_ref,
                gate_state.value, gate_state.value, None,
            ))

        # Detect named order signals as transitions
        if order_signal == "ENTRY":
            events.append((
                CFG.strategy_id, self._ticker, self._session_date,
                ts_ns, "RISING_EDGE",
                hawkes_state.lambda_hat, self._lambda_ref,
                prev_gate_state.value, gate_state.value, None,
            ))
        elif order_signal == "EXIT_D":
            events.append((
                CFG.strategy_id, self._ticker, self._session_date,
                ts_ns, "EXIT_D_FIRE",
                hawkes_state.lambda_hat, self._lambda_ref,
                gate_state.value, gate_state.value, None,
            ))
        elif order_signal == "LULD":
            events.append((
                CFG.strategy_id, self._ticker, self._session_date,
                ts_ns, "LULD_PROXIMITY_FIRE",
                hawkes_state.lambda_hat, self._lambda_ref,
                gate_state.value, gate_state.value, None,
            ))

        self._prev_gate_state = gate_state

        return SignalResult(
            sip_timestamp=ts_ns,
            lambda_buy=hawkes_state.lambda_buy,
            lambda_sell=hawkes_state.lambda_sell,
            lambda_hat=hawkes_state.lambda_hat,
            gate_state=gate_state.value,
            q_tilde=self._current_q_tilde,
            order_signal=order_signal,
            is_trade=True,
            side=side,
            signal_events=events,
            hawkes_refit_record=refit_record,
        )

    def _lee_ready(self, price: float) -> int:
        """0 = BUY, 1 = SELL. Last known quote; tick test if quote unavailable."""
        if self._last_bid is not None and self._last_ask is not None:
            if price >= self._last_ask:
                return 0
            if price <= self._last_bid:
                return 1
        # Tick test fallback
        if self._sf_prices and price > self._sf_prices[-1]:
            return 0
        if self._sf_prices and price < self._sf_prices[-1]:
            return 1
        return 0

    def _recompute_setup_filter(self) -> None:
        if not self._sf_ts:
            return
        try:
            ts_arr = np.array(self._sf_ts, dtype=np.int64)
            p_arr = np.array(self._sf_prices, dtype=np.float64)
            s_arr = np.array(self._sf_sizes, dtype=np.int64)
            sf = run_setup_filter(
                timestamps=ts_arr,
                prices=p_arr,
                sizes=s_arr,
                session_start_ns=self._session_start_ns,
                session_end_ns=self._session_end_ns,
            )
            self._current_sf_passes = sf.passes
            self._current_q_tilde = float(sf.q_tilde[-1]) if len(sf.q_tilde) > 0 else None
        except Exception:
            log.exception("%s: setup filter recompute failed", self._ticker)

    def _maybe_refit(self, ts_ns: int) -> Optional[tuple]:
        if self._fitted_params is None:
            return None
        buf = list(self._refit_buffer)
        if len(buf) < 100:
            return None
        t_arr = np.array([x[0] for x in buf], dtype=np.float64)
        s_arr = np.array([x[1] for x in buf], dtype=np.int32)
        try:
            new_params = fit_online(
                t_sec=t_arr,
                sides=s_arr,
                rho=CFG.hawkes.rho,
                lambda_ref=self._lambda_ref,
                prev_params=self._fitted_params,
                T=float(t_arr[-1]),
                n_restarts=1,
                beta_fixed=CFG.hawkes.beta,
            )
            self._engine.swap_params(
                new_params.alpha_buy_self,
                new_params.alpha_sell_self,
                new_params.mu_buy,
                new_params.mu_sell,
            )
            self._fitted_params = new_params
            self._refit_count += 1
            log.debug("%s: refit #%d mu_buy=%.4f mu_sell=%.4f",
                      self._ticker, self._refit_count, new_params.mu_buy, new_params.mu_sell)
            return (
                CFG.strategy_id, self._ticker, self._session_date,
                ts_ns, self._refit_count, self._trade_count,
                new_params.mu_buy, new_params.mu_sell,
                new_params.alpha_buy_self, new_params.alpha_sell_self,
                getattr(new_params, "n_base", None),
                getattr(new_params, "log_likelihood", None),
            )
        except Exception:
            log.exception("%s: online refit failed", self._ticker)
            return None

    def _evaluate_signals(
        self,
        hawkes_state: HawkesState,
        gate_state: GateState,
        luld_result,
        price: float,
        t_sec: float,
        ts_ns: int,
    ) -> Optional[str]:
        bkt = session_bucket(t_sec)

        if self._in_position:
            return self._check_exits(hawkes_state, gate_state, luld_result, t_sec, bkt)
        else:
            return self._check_entry(hawkes_state, gate_state, price, bkt)

    def _check_entry(
        self,
        hawkes_state: HawkesState,
        gate_state: GateState,
        price: float,
        bkt: str,
    ) -> Optional[str]:
        # EPG rising edge: transition from non-PASS → PASS
        rising_edge = (gate_state == GateState.PASS and self._prev_gate_state != GateState.PASS)
        if not rising_edge:
            return None

        # Gap gate
        if self._intraday_pct < CFG.scanner.gap_threshold:
            return None

        # Setup filter
        if not self._current_sf_passes:
            return None

        return "ENTRY"

    def _check_exits(
        self,
        hawkes_state: HawkesState,
        gate_state: GateState,
        luld_result,
        t_sec: float,
        bkt: str,
    ) -> Optional[str]:
        # EXIT_D (first priority)
        if self._check_exit_d(hawkes_state, t_sec, bkt):
            return "EXIT_D"

        # LULD proximity (RTH only per config)
        if not CFG.luld.rth_only or bkt == "regular_hours":
            if luld_result.state == ProximityState.EXIT_HALT:
                return "LULD"

        # EPG window close: PASS → FAIL or INACTIVE
        if self._prev_gate_state == GateState.PASS and gate_state in (
            GateState.FAIL, GateState.INACTIVE
        ):
            return "EPG_CLOSE"

        return None

    def _check_exit_d(
        self, hawkes_state: HawkesState, t_sec: float, bkt: str
    ) -> bool:
        # Disabled pre-market (pre_market_override=False means "disabled pre-market")
        if bkt == "pre_market" and not CFG.exit_d.pre_market_override:
            self._exit_d_timer_start = None
            return False

        total = hawkes_state.lambda_buy + hawkes_state.lambda_sell
        if total < 1e-10:
            self._exit_d_timer_start = None
            return False

        imbalance = hawkes_state.lambda_sell / total

        # Disabled if I(t) at entry was already > theta
        if self._i_entry is not None and self._i_entry > CFG.exit_d.theta:
            return False

        if imbalance > CFG.exit_d.theta:
            if self._exit_d_timer_start is None:
                self._exit_d_timer_start = t_sec
            elif t_sec - self._exit_d_timer_start >= CFG.exit_d.tau_min_sec:
                return True
        else:
            self._exit_d_timer_start = None

        return False

    @property
    def in_position(self) -> bool:
        return self._in_position

    @property
    def ticker(self) -> str:
        return self._ticker

    @property
    def intraday_pct(self) -> float:
        return self._intraday_pct

    @property
    def scanner_context(self) -> dict:
        return self._scanner_context

    def current_imbalance(self) -> float:
        """Last computed I(t) — called by signal_loop to record i_entry."""
        return self._last_imbalance

    @property
    def last_lambda_hat(self) -> float:
        return self._last_lambda_hat

    @property
    def last_lambda_ref(self) -> float:
        return self._lambda_ref

    @property
    def last_price(self) -> float:
        return self._sf_prices[-1] if self._sf_prices else 0.0

    @property
    def epg_gate_state(self) -> str:
        return self._prev_gate_state.value
