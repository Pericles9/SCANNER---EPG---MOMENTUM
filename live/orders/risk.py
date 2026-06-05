"""Risk state, order request types, and position sizing."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from live.config import CFG

log = logging.getLogger(__name__)


@dataclass
class OrderRequest:
    ticker: str
    side: str               # 'BUY' or 'SELL'
    qty: int
    session_bucket: str     # 'pre_market', 'regular_hours', 'post_market'
    is_entry: bool
    exit_reason: Optional[str] = None  # 'EXIT_D', 'LULD', 'EPG_CLOSE', 'KILL', 'EOD'
    limit_price: Optional[float] = None  # set for pre-market limit orders
    intraday_pct: float = 0.0           # for DB record
    expected_price: float = 0.0         # signal-time price estimate for slippage tracking
    on_fill_confirmed: Optional[Callable] = field(default=None, repr=False)
    on_fill_failed: Optional[Callable] = field(default=None, repr=False)


@dataclass
class FlattenAllRequest:
    """Sentinel: flatten all open positions immediately (dead man's switch / kill)."""
    reason: str = "dead_mans_switch"


@dataclass
class RiskState:
    daily_pnl: float = 0.0
    open_positions: dict = field(default_factory=dict)  # ticker → {qty, avg_cost}
    pending_close: set = field(default_factory=set)           # tickers with in-flight or failed exits
    pending_close_failures: dict = field(default_factory=dict) # ticker → consecutive failed flatten attempts
    manual_review_required: set = field(default_factory=set)   # tickers crash recovery could not auto-flatten
    max_daily_loss: float = field(default_factory=lambda: CFG.risk.max_daily_loss)
    max_concurrent: int = field(default_factory=lambda: CFG.risk.max_concurrent_positions)
    account_equity: float = 0.0        # refreshed from IBKR every 5 minutes
    account_buying_power: float = 0.0  # refreshed from IBKR every 5 minutes
    theoretical_equity: float = 0.0   # compounding strategy equity curve
    _loss_limit_hit: bool = field(default=False, init=False, repr=False)
    _auto_kill_fired: bool = field(default=False, init=False, repr=False)
    _trade_history: list = field(default_factory=list, init=False, repr=False)

    def allows(self, request: OrderRequest) -> bool:
        if not request.is_entry:
            return True  # exits are never blocked by risk limits
        if self._loss_limit_hit:
            return False
        if self.daily_pnl <= self.max_daily_loss:
            self._loss_limit_hit = True
            log.warning("Daily loss limit hit: %.2f <= %.2f — blocking new entries",
                        self.daily_pnl, self.max_daily_loss)
            return False
        if len(self.open_positions) >= self.max_concurrent:
            log.warning("Max concurrent positions (%d) reached — blocking entry for %s",
                        self.max_concurrent, request.ticker)
            return False
        return True

    def record_fill(
        self,
        ticker: str,
        side: str,
        qty: int,
        fill_price: float,
        filled_qty: Optional[int] = None,
    ) -> None:
        actual_qty = filled_qty if filled_qty is not None else qty
        if side == "BUY":
            # Aggregate with existing position (weighted-average cost). Without this,
            # a second BUY on the same ticker silently overwrites the first, leaving
            # phantom shares at IBKR that risk_state doesn't know about — which then
            # under-sells on the SELL exit, leaking a partial position into the next
            # session.
            existing = self.open_positions.get(ticker)
            if existing is None:
                self.open_positions[ticker] = {"qty": actual_qty, "avg_cost": fill_price}
            else:
                new_qty = existing["qty"] + actual_qty
                new_avg = (
                    (existing["qty"] * existing["avg_cost"]) + (actual_qty * fill_price)
                ) / new_qty
                self.open_positions[ticker] = {"qty": new_qty, "avg_cost": new_avg}
        elif side == "SELL":
            pos = self.open_positions.pop(ticker, None)
            if pos:
                pnl = (fill_price - pos["avg_cost"]) * pos["qty"]
                self.daily_pnl += pnl
                avg_cost = pos["avg_cost"]
                pnl_pct = (fill_price - avg_cost) / avg_cost if avg_cost > 0 else 0.0
                self._trade_history.append(pnl_pct)
                if self.theoretical_equity > 0:
                    self.theoretical_equity *= (1.0 + pnl_pct)
                log.info(
                    "Position closed: %s PnL=%.2f daily=%.2f theo_equity=%.2f",
                    ticker, pnl, self.daily_pnl, self.theoretical_equity,
                )

    def has_position(self, ticker: str) -> bool:
        return ticker in self.open_positions

    def compute_kelly_notional(self) -> float:
        """Fractional Kelly notional. Falls back to flat RTH notional if insufficient history."""
        lookback = CFG.position_sizing.kelly_lookback_trades
        min_sample = CFG.position_sizing.kelly_min_sample
        history = self._trade_history[-lookback:]
        if len(history) < min_sample:
            log.debug("Kelly: insufficient history (%d < %d) — using flat notional",
                      len(history), min_sample)
            return CFG.position_sizing.rth_notional

        wins = [p for p in history if p > 0]
        losses = [p for p in history if p < 0]
        if not wins or not losses:
            return CFG.position_sizing.rth_notional

        win_rate = len(wins) / len(history)
        avg_win = sum(wins) / len(wins)
        avg_loss = abs(sum(losses) / len(losses))

        # Kelly formula: f = win_rate/avg_loss - (1-win_rate)/avg_win
        kelly_f = (win_rate / avg_loss) - ((1.0 - win_rate) / avg_win)
        kelly_f = max(0.0, kelly_f)
        notional = kelly_f * CFG.position_sizing.kelly_fraction * max(self.account_equity, 1.0)

        # Clamp: 0.25x–5x flat notional to prevent extreme sizing
        flat = CFG.position_sizing.rth_notional
        notional = max(flat * 0.25, min(flat * 5.0, notional))
        log.debug("Kelly: f=%.4f fraction=%.2f equity=%.0f notional=%.0f",
                  kelly_f, CFG.position_sizing.kelly_fraction,
                  self.account_equity, notional)
        return notional

    def compute_position_size(self, price: float, session_bucket: str) -> int:
        """Return share qty for the given price and session bucket."""
        if CFG.position_sizing.mode == "buying_power":
            buying_power = self.account_buying_power if self.account_buying_power > 0 \
                           else self.account_equity * CFG.position_sizing.leverage
            notional = buying_power / max(self.max_concurrent, 1)
        elif CFG.position_sizing.mode == "kelly":
            notional = self.compute_kelly_notional()
        elif session_bucket == "pre_market":
            notional = CFG.position_sizing.pre_market_notional
        else:
            notional = CFG.position_sizing.rth_notional
        return max(1, int(notional / price)) if price > 0 else 1
