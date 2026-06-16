"""Startup reconstruction of in-memory daily state from the DB.

A mid-session restart (crash recovery / redeploy) rebuilds `RiskState` from scratch
(`daily_pnl=0`, empty Kelly history, `theoretical_equity` reset, loss-limit cleared). The
persisted `trades` / `sessions` tables, however, still hold the real figures. This module
re-derives the in-memory aggregates from those tables so the Telegram readouts (`/status`,
`/risk`, `/summary`) stay continuous across restarts and Kelly sizing keeps its history.

Positions are deliberately NOT reconstructed here — crash recovery flattens all open positions
on startup, so `risk_state.open_positions` correctly reflects the post-recovery (flat) state.
"""
from __future__ import annotations

import logging
from datetime import date

log = logging.getLogger(__name__)


async def reconstruct_daily_state(
    risk_state,
    pool,
    session_date: date,
    strategy_id: str,
    kelly_lookback: int,
) -> None:
    """Rebuild realised P&L, Kelly trade history, loss-limit flag, and theoretical equity
    from the DB for `session_date` / `strategy_id`. Best-effort — never raises."""
    try:
        async with pool.acquire() as conn:
            today = await conn.fetchrow(
                """
                SELECT coalesce(sum(pnl_dollar), 0.0) AS pnl, count(*) AS n
                FROM trades
                WHERE strategy_id=$1 AND session_date=$2
                """,
                strategy_id, session_date,
            )
            # Kelly history: last N closed trades (any day), oldest→newest.
            hist_rows = await conn.fetch(
                """
                SELECT pnl_pct FROM trades
                WHERE strategy_id=$1 AND pnl_pct IS NOT NULL
                ORDER BY exit_ns DESC NULLS LAST
                LIMIT $2
                """,
                strategy_id, int(kelly_lookback),
            )
            theo = await conn.fetchval(
                """
                SELECT theoretical_equity_end FROM sessions
                WHERE strategy_id=$1 AND theoretical_equity_end IS NOT NULL
                ORDER BY session_date DESC, id DESC
                LIMIT 1
                """,
                strategy_id,
            )
    except Exception:
        log.exception("State recovery: failed to reconstruct daily state — starting flat")
        return

    daily_pnl = float(today["pnl"] or 0.0)
    n_trades = int(today["n"] or 0)
    risk_state.daily_pnl = daily_pnl
    # Stored DESC (newest first) → reverse to oldest→newest, matching live append order.
    risk_state._trade_history = [
        float(r["pnl_pct"]) for r in reversed(hist_rows) if r["pnl_pct"] is not None
    ]
    # Re-arm the loss-limit block if the persisted realised P&L is already past the limit.
    if daily_pnl <= risk_state.max_daily_loss:
        risk_state._loss_limit_hit = True
        log.warning(
            "State recovery: realised PnL %.2f <= limit %.2f — loss limit re-armed at startup",
            daily_pnl, risk_state.max_daily_loss,
        )
    # Carry the synthetic Kelly equity curve forward (else keep the account-equity seed).
    if theo is not None and float(theo) > 0:
        risk_state.theoretical_equity = float(theo)

    log.info(
        "State recovery: restored daily_pnl=%.2f from %d trade(s), kelly_history=%d, theo_equity=%.2f",
        risk_state.daily_pnl, n_trades, len(risk_state._trade_history),
        risk_state.theoretical_equity,
    )
