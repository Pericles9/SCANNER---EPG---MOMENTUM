"""VwapSignalState: per-ticker VWAP strategy state machine.

Entry: first 1-minute bar close strictly above running VWAP (bar-close confirmation).
Exit: first bar close strictly below VWAP (VWAP_CLOSE) or intra-bar price ≤ entry × (1 − hard_stop_pct).
One-shot per session: record_exit() marks CLOSED; next update_trade() returns session_done=True.
"""
from __future__ import annotations

import logging
import time
from datetime import date
from typing import Optional

import numpy as np

from backtest.runner import session_bucket
from backtest.setup_filter import run_setup_filter

from live.config import CFG
from live.signals.context_fetch import ContextFetchResult
from live.signals.live_state import SignalResult

log = logging.getLogger(__name__)

_NS_PER_SEC = 1_000_000_000


class VwapSignalState:
    """VWAP strategy state machine.  State: FLAT → LONG → CLOSED (terminal)."""

    def __init__(
        self,
        ticker: str,
        ctx: ContextFetchResult,
        scanner_context: dict,
        session_date: date,
        risk_state,   # RiskState — read-only reference for HARD_STOP fill price
    ) -> None:
        self._ticker = ticker
        self._session_date = session_date
        self._session_start_ns: int = ctx.session_start_ns
        self._session_end_ns: int = ctx.session_end_ns
        self._risk_state = risk_state

        # VWAP accumulators
        self._pv_sum: float = 0.0
        self._v_sum: float = 0.0
        self._last_vwap: float = 0.0
        self._last_price: float = 0.0

        # Bar-boundary tracking (matches LiveSignalState pattern)
        self._last_bar_minute: int = -1

        # Per-bucket anchor: track current session bucket to detect transitions
        self._cur_bucket: str = ""

        # SF buffer — pre-populated from historical ticks (matches LiveSignalState layout)
        self._sf_ts: list[int] = []
        self._sf_prices: list[float] = []
        self._sf_sizes: list[int] = []

        # Dedup boundary (last seeded timestamp; first live tick must be strictly after)
        self._last_ts_ns: int = ctx.last_ts_ns

        # State machine
        self._state: str = "FLAT"   # "FLAT" | "LONG" | "CLOSED"
        self._in_position: bool = False
        self._exit_signaled: bool = False
        self._session_done: bool = False
        # Latches True once the entry fill is observed in risk_state. Until then a
        # missing position means the entry is still filling (record_entry sets LONG
        # optimistically when the order is queued) — NOT an external close. Gating
        # the external-close check on this prevents orphaning a just-opened position.
        self._position_confirmed: bool = False

        # SF config (mirrors LiveSignalState)
        self._sf_q_threshold: float = CFG.setup_filter.q_threshold
        self._sf_warmup_threshold: float = CFG.setup_filter.warmup_provisional_threshold
        self._sf_warmup_bars: int = CFG.setup_filter.warmup_bars
        self._sf_removal_bars: int = CFG.setup_filter.removal_bars
        self._sf_fail_bars: int = 0
        self._sf_disqualified: bool = False

        # Armed: True when setup filter passes (or always if setup_filter_gate=False)
        self._armed: bool = False

        # Last known quote for order construction
        self._last_bid: Optional[float] = None
        self._last_ask: Optional[float] = None

        # Authoritative display mark — dedicated last-trade field, decoupled from the
        # strategy's internal _last_price (used for VWAP/bar-close). Never forces a $0
        # display. Seeded in _seed_tick; updated on every live trade.
        self._last_trade_price: Optional[float] = None
        self._last_trade_ts_ns: int = 0
        # Real exchange LULD state (Massive WS) — detection / display / safety only.
        self._halted: bool = False          # used by mark()
        self._luld_bands: tuple[Optional[float], Optional[float]] = (None, None)
        self._luld_indicators: list[int] = []
        self._halt_started_ns: int = 0
        self._luld_last_seen: float = 0.0   # monotonic; halt-suspected detection (Phase 3)

        # Scanner context
        self._intraday_pct: float = scanner_context.get("pct_change", 0.0) / 100.0
        self._scanner_context: dict = scanner_context

        self._seed_from_context(ctx)

    # ─── Seeding ────────────────────────────────────────────────────────────────

    def _seed_from_context(self, ctx: ContextFetchResult) -> None:
        """Seed VWAP and SF buffer from historical arrays. No orders emitted."""
        ts_arr = ctx.tick_timestamps_ns
        p_arr = ctx.tick_prices
        s_arr = ctx.tick_sizes

        for i in range(len(ts_arr)):
            self._seed_tick(int(ts_arr[i]), float(p_arr[i]), int(s_arr[i]))

        # Set armed from the pre-computed setup_filter_result on the context
        sf = ctx.setup_filter_result
        if CFG.scanner_vwap.setup_filter_gate:
            self._armed = self._sf_admit(sf.q_tilde) if (sf and len(sf.q_tilde) > 0) else False
        else:
            self._armed = True

        # Set _last_bar_minute from the last seeded tick so bar-boundary fires correctly
        if self._sf_ts:
            self._last_bar_minute = self._sf_ts[-1] // (_NS_PER_SEC * 60)

    def _seed_tick(self, ts_ns: int, price: float, size: int) -> None:
        """Seed one tick: accumulate VWAP and populate SF buffer. No signals."""
        t_sec = (ts_ns - self._session_start_ns) / _NS_PER_SEC
        bkt = session_bucket(t_sec)

        if CFG.scanner_vwap.vwap_anchor == "per_bucket":
            if self._cur_bucket != "" and bkt != self._cur_bucket:
                self._pv_sum = 0.0
                self._v_sum = 0.0
            self._cur_bucket = bkt
            self._pv_sum += price * size
            self._v_sum += size
        elif CFG.scanner_vwap.vwap_anchor == "rth_only":
            if bkt == "regular_hours":
                self._pv_sum += price * size
                self._v_sum += size

        if self._v_sum > 0:
            self._last_vwap = self._pv_sum / self._v_sum
        self._last_price = price
        if price > 0:
            self._last_trade_price = price
            self._last_trade_ts_ns = ts_ns

        self._sf_ts.append(ts_ns)
        self._sf_prices.append(price)
        self._sf_sizes.append(size)

    # ─── Main tick entry-point ───────────────────────────────────────────────────

    def update_trade(self, tick: dict) -> SignalResult:
        """Process a trade tick. Returns SignalResult possibly containing an order signal."""
        ts_ns: int = tick.get("t", 0)

        if ts_ns <= self._last_ts_ns:
            return self._silent(ts_ns)
        self._last_ts_ns = ts_ns

        # Terminal state: signal session done so signal_loop calls session_done_callback
        if self._state == "CLOSED":
            return SignalResult(
                sip_timestamp=ts_ns,
                lambda_buy=0.0, lambda_sell=0.0, lambda_hat=0.0,
                gate_state="inactive", q_tilde=None,
                order_signal=None, is_trade=True,
                disqualify=True, session_done=True,
            )

        price: float = tick.get("p", 0.0)
        size: int = tick.get("s", 0)
        t_sec = (ts_ns - self._session_start_ns) / _NS_PER_SEC
        bkt = session_bucket(t_sec)
        events: list = []
        order_signal: Optional[str] = None

        bar_minute = ts_ns // (_NS_PER_SEC * 60)
        is_new_bar = (bar_minute != self._last_bar_minute) and (self._last_bar_minute != -1)

        # Bar-close: use previous bar's last price and accumulated VWAP
        if is_new_bar:
            sig, bar_events = self._on_bar_close(self._last_price, self._last_vwap, ts_ns)
            if sig is not None:
                order_signal = sig
            events.extend(bar_events)

        self._last_bar_minute = bar_minute

        # SF buffer append (matches LiveSignalState: append first, then recompute)
        self._sf_ts.append(ts_ns)
        self._sf_prices.append(price)
        self._sf_sizes.append(size)

        # SF recompute at bar boundary (after append, same as LiveSignalState)
        if is_new_bar and CFG.scanner_vwap.setup_filter_gate:
            arm_events = self._recompute_setup_filter(ts_ns)
            events.extend(arm_events)

        # VWAP update
        if CFG.scanner_vwap.vwap_anchor == "per_bucket":
            if self._cur_bucket != "" and bkt != self._cur_bucket:
                self._pv_sum = 0.0
                self._v_sum = 0.0
            self._cur_bucket = bkt
            self._pv_sum += price * size
            self._v_sum += size
        elif CFG.scanner_vwap.vwap_anchor == "rth_only":
            if bkt == "regular_hours":
                self._pv_sum += price * size
                self._v_sum += size

        if self._v_sum > 0:
            self._last_vwap = self._pv_sum / self._v_sum
        self._last_price = price
        if price > 0:
            self._last_trade_price = price
            self._last_trade_ts_ns = ts_ns

        # HARD_STOP: intra-bar check, only when LONG and no exit in-flight
        if self._state == "LONG" and not self._exit_signaled and order_signal is None:
            fp = self._risk_state.open_positions.get(self._ticker, {}).get("avg_cost")
            if fp is not None and fp > 0:
                stop_level = fp * (1.0 - CFG.scanner_vwap.hard_stop_pct)
                if price <= stop_level:
                    order_signal = "HARD_STOP"
                    self._exit_signaled = True
                    events.append((
                        CFG.strategy_id, self._ticker, self._session_date,
                        ts_ns, "HARD_STOP",
                        None, None, None, None,
                        f"price={price:.4f} stop_level={stop_level:.4f} entry={fp:.4f}",
                    ))

        # Tick-level VWAP cross exit (tick mode only): exit as soon as price drops below VWAP
        if (self._state == "LONG" and not self._exit_signaled and order_signal is None
                and CFG.scanner_vwap.vwap_exit_mode == "tick"
                and self._last_vwap > 0.0 and price < self._last_vwap):
            order_signal = "VWAP_CROSS"
            self._exit_signaled = True
            events.append((
                CFG.strategy_id, self._ticker, self._session_date,
                ts_ns, "VWAP_EXIT",
                None, None, None, None,
                f"price={price:.4f} vwap={self._last_vwap:.4f} mode=tick",
            ))

        # Confirm the entry fill has landed in risk_state. Latches once, so a later
        # disappearance is a genuine external close rather than fill latency.
        if self._state == "LONG" and self._risk_state.has_position(self._ticker):
            self._position_confirmed = True

        # External close reconciliation (kill switch, EOD, manual flatten). Only valid
        # AFTER the position was confirmed open — otherwise the entry-fill-in-flight
        # window (optimistic record_entry → LONG before the BUY fills) is misread as an
        # external close, which orphaned the just-opened position (no ctx, locked out).
        if (self._state == "LONG" and self._position_confirmed
                and not self._risk_state.has_position(self._ticker)):
            log.warning("%s: external close detected — marking CLOSED", self._ticker)
            self._state = "CLOSED"
            self._in_position = False
            self._session_done = True

        should_disqualify = self._sf_disqualified and self._state == "FLAT"

        return SignalResult(
            sip_timestamp=ts_ns,
            lambda_buy=0.0, lambda_sell=0.0, lambda_hat=0.0,
            gate_state="inactive", q_tilde=None,
            order_signal=order_signal,
            is_trade=True,
            disqualify=should_disqualify,
            session_done=False,
            signal_events=events,
        )

    def update_quote(self, quote: dict) -> SignalResult:
        self._last_bid = quote.get("bp")
        self._last_ask = quote.get("ap")
        ts_ns = quote.get("t", 0)
        return self._silent(ts_ns)

    # ─── Bar-close logic ────────────────────────────────────────────────────────

    def _on_bar_close(
        self, close: float, vwap: float, ts_ns: int
    ) -> tuple[Optional[str], list]:
        """Entry/exit decisions at 1-minute bar close. Returns (signal, events)."""
        if close <= 0.0 or vwap <= 0.0:
            return None, []

        # Entry: FLAT + armed + bar close strictly above VWAP
        if self._state == "FLAT" and self._armed and close > vwap:
            return "ENTRY", [(
                CFG.strategy_id, self._ticker, self._session_date,
                ts_ns, "VWAP_ENTRY",
                None, None, None, None,
                f"close={close:.4f} vwap={vwap:.4f}",
            )]

        # Exit: bar_close mode only — tick mode uses per-tick check instead
        if (CFG.scanner_vwap.vwap_exit_mode == "bar_close"
                and self._state == "LONG" and not self._exit_signaled and close < vwap):
            return "VWAP_CLOSE", [(
                CFG.strategy_id, self._ticker, self._session_date,
                ts_ns, "VWAP_EXIT",
                None, None, None, None,
                f"close={close:.4f} vwap={vwap:.4f} mode=bar_close",
            )]

        return None, []

    # ─── Setup filter ───────────────────────────────────────────────────────────

    def _sf_admit(self, q_tilde) -> bool:
        n = len(q_tilde)
        if n == 0:
            return False
        thr = self._sf_warmup_threshold if n < self._sf_warmup_bars else self._sf_q_threshold
        return float(q_tilde[-1]) >= thr

    def _recompute_setup_filter(self, ts_ns: int) -> list:
        """Re-run setup filter at bar boundary. Returns list of signal_events tuples."""
        if not self._sf_ts:
            return []
        events = []
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
            q_last = float(sf.q_tilde[-1]) if len(sf.q_tilde) > 0 else None

            prev_armed = self._armed
            self._armed = self._sf_admit(sf.q_tilde)

            if self._armed and not prev_armed:
                events.append((
                    CFG.strategy_id, self._ticker, self._session_date,
                    ts_ns, "VWAP_ARMED",
                    None, None, None, None,
                    f"q_tilde={q_last:.4f} threshold={self._sf_q_threshold:.2f}",
                ))

            if q_last is not None and q_last >= self._sf_q_threshold:
                self._sf_fail_bars = 0
            else:
                self._sf_fail_bars += 1
                if self._sf_fail_bars >= self._sf_removal_bars and not self._sf_disqualified:
                    self._sf_disqualified = True
                    log.warning(
                        "%s: SF disqualified — q̃ < %.2f for %d consecutive bars",
                        self._ticker, self._sf_q_threshold, self._sf_fail_bars,
                    )
        except Exception:
            log.exception("%s: setup filter recompute failed", self._ticker)
        return events

    # ─── Protocol methods ────────────────────────────────────────────────────────

    def record_entry(self, session_bkt: str, i_entry: float) -> None:
        """Called by signal_loop after entry order is queued (optimistic)."""
        self._state = "LONG"
        self._in_position = True
        self._exit_signaled = False

    def signal_exit(self) -> None:
        """Called by signal_loop when an exit order is queued. Suppresses duplicates."""
        self._exit_signaled = True

    def record_exit(self) -> None:
        """Called by order_worker after exit fill confirms."""
        self._state = "CLOSED"
        self._in_position = False
        self._session_done = True

    def clear_exit_pending(self) -> None:
        """Called by order_worker when exit order fails. Re-arms exit signal."""
        self._exit_signaled = False

    def freeze(self) -> None:
        pass

    def resume(self, halt_duration_sec: float = 0.0) -> None:
        pass

    def is_halted(self) -> bool:
        """True while a real exchange (Massive LULD) halt is in effect."""
        return self._halted

    def update_luld(self, msg: dict) -> Optional[tuple[str, float]]:
        """Process a Massive LULD event. Detection / display / safety ONLY — never
        emits an order signal. Indicators 17 (halt) / 18 (resume) are NASDAQ-only
        (z == 3); other tapes carry only bands and are inferred in the heartbeat
        monitor (Phase 3). `t` is already ns (normalised at dispatch). Returns
        ('HALT', 0.0) or ('RESUME', duration_sec) on a state transition, else None.
        """
        h = msg.get("h")
        l = msg.get("l")
        self._luld_bands = (h, l)
        indicators = msg.get("i") or []
        self._luld_indicators = list(indicators)
        self._luld_last_seen = time.monotonic()
        ts_ns = msg.get("t", 0)

        if 17 in indicators and not self._halted:
            self._halted = True
            self._halt_started_ns = ts_ns
            return ("HALT", 0.0)
        if 18 in indicators and self._halted:
            self._halted = False
            duration = (
                max(0.0, (ts_ns - self._halt_started_ns) / 1e9)
                if self._halt_started_ns else 0.0
            )
            return ("RESUME", duration)
        return None

    def current_imbalance(self) -> float:
        return 0.5

    # ─── Properties ─────────────────────────────────────────────────────────────

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

    @property
    def last_bid(self) -> Optional[float]:
        return self._last_bid

    @property
    def last_ask(self) -> Optional[float]:
        return self._last_ask

    def mark(self, now_ns: int, stale_s: float = 5.0) -> tuple[Optional[float], str, float]:
        """Return (price, source, age_s) for display/P&L. Never returns 0.0 as a real price.

        source ∈ {'LIVE','MID','STALE','HALTED','NONE'}. Same priority as
        LiveSignalState.mark: fresh last-trade → LIVE; else two-sided mid → MID; else
        stale last-trade → STALE (or HALTED while halted); else (None,'NONE',0.0).
        """
        ltp = self._last_trade_price
        age_s = 0.0
        if ltp is not None and ltp > 0:
            age_s = max(0.0, (now_ns - self._last_trade_ts_ns) / 1e9)
            if age_s <= stale_s:
                return ltp, "LIVE", age_s
        if self._last_bid and self._last_ask and self._last_bid > 0 and self._last_ask > 0:
            return (self._last_bid + self._last_ask) / 2.0, "MID", 0.0
        if ltp is not None and ltp > 0:
            return ltp, ("HALTED" if self._halted else "STALE"), age_s
        return None, "NONE", 0.0

    @property
    def last_price(self) -> Optional[float]:
        """Backward-compatible mark. Never a silent 0.0: prefers last trade, then a
        two-sided mid, then the strategy's internal last price; None when unknown."""
        if self._last_trade_price is not None and self._last_trade_price > 0:
            return self._last_trade_price
        if self._last_bid and self._last_ask and self._last_bid > 0 and self._last_ask > 0:
            return (self._last_bid + self._last_ask) / 2.0
        if self._last_price > 0:
            return self._last_price
        return None

    @property
    def epg_gate_state(self) -> str:
        return "inactive"

    @property
    def luld_bands(self) -> tuple[Optional[float], Optional[float]]:
        """Last known LULD (upper, lower) price bands. (None, None) until first LULD event."""
        return self._luld_bands

    @property
    def luld_last_seen(self) -> float:
        """Monotonic time of the last LULD event for this ticker (0.0 = never)."""
        return self._luld_last_seen

    @property
    def last_lambda_hat(self) -> float:
        return 0.0

    @property
    def last_lambda_ref(self) -> float:
        return 0.0

    # ─── Helpers ────────────────────────────────────────────────────────────────

    def _silent(self, ts_ns: int) -> SignalResult:
        return SignalResult(
            sip_timestamp=ts_ns,
            lambda_buy=0.0, lambda_sell=0.0, lambda_hat=0.0,
            gate_state="inactive", q_tilde=None,
            order_signal=None, is_trade=True,
        )
