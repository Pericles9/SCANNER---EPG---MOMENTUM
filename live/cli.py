"""Status CLI for the EPG live paper trading system.

Used by:
  - Telegram /status command (via get_system_status callback)
  - Direct CLI: python -m live.cli

Queries the live DB and RiskState for current system state.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date
from typing import Optional

from live.config import CFG


async def get_system_status(risk_state, pool) -> str:
    """Build a status summary string. Called by Telegram /status handler."""
    session_date = date.today()

    async with pool.acquire() as conn:
        open_pos_rows = await conn.fetch(
            """
            SELECT ticker, qty, avg_entry_price, open_ns
            FROM positions
            WHERE strategy_id=$1 AND session_date=$2
            """,
            CFG.strategy_id, session_date,
        )
        trade_rows = await conn.fetch(
            """
            SELECT count(*) AS n, coalesce(sum(pnl_dollar), 0) AS total_pnl
            FROM trades
            WHERE strategy_id=$1 AND session_date=$2
            """,
            CFG.strategy_id, session_date,
        )
        session_rows = await conn.fetch(
            """
            SELECT ticker, degraded_mode, cold_start_n
            FROM sessions
            WHERE strategy_id=$1 AND session_date=$2
            ORDER BY id DESC
            LIMIT 10
            """,
            CFG.strategy_id, session_date,
        )

    lines = [
        f"EPG Live — {session_date.isoformat()}",
        f"Daily PnL:    ${risk_state.daily_pnl:+.2f}",
        f"Loss limit:   {'HIT' if risk_state._loss_limit_hit else 'OK'} (limit=${CFG.risk.max_daily_loss:.0f})",
        f"Account EQ:   ${risk_state.account_equity:,.0f}",
        f"Theo EQ:      ${risk_state.theoretical_equity:,.2f}",
    ]

    lines.append("")
    lines.append(f"Open positions ({len(risk_state.open_positions)}):")
    if risk_state.open_positions:
        for ticker, pos in risk_state.open_positions.items():
            lines.append(f"  {ticker}: {pos['qty']} sh @ ${pos['avg_cost']:.2f}")
    else:
        lines.append("  (none)")

    if trade_rows:
        n = trade_rows[0]["n"]
        pnl = trade_rows[0]["total_pnl"]
        lines.append("")
        lines.append(f"Trades today: {n} | realized PnL: ${pnl:+.2f}")

    if session_rows:
        lines.append("")
        lines.append(f"Sessions today ({len(session_rows)}):")
        for r in session_rows:
            deg = " [DEGRADED]" if r["degraded_mode"] else ""
            lines.append(f"  {r['ticker']}: {r['cold_start_n']} ticks at open{deg}")

    return "\n".join(lines)


async def _cli_main() -> None:
    """Entry point for command-line status query."""
    from live.db.pool import get_pool, init_pool
    from live.orders.risk import RiskState

    pool = await init_pool()
    risk_state = RiskState()

    print(await get_system_status(risk_state, pool))

    from live.db.pool import close_pool
    await close_pool()


if __name__ == "__main__":
    asyncio.run(_cli_main())
