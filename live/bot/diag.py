"""Phase 0 diagnostic instrumentation for the $0-mark failure mode.

One-shot DEBUG dump fired only when an open position resolves to a mark of 0 or
None. Purely diagnostic — no logic changes, no effect on the order/exit path.
Each (where, ticker) pair logs at most once per process to avoid log spam.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

# (where, ticker) pairs already dumped this process — one-shot guard.
_DUMPED: set[tuple[str, str]] = set()


def _age_s(monotonic_t: Optional[float]) -> Optional[float]:
    if not monotonic_t:
        return None
    return time.monotonic() - monotonic_t


async def dump_zero_mark(
    where: str,
    ticker: str,
    signal_state,
    in_universe: bool,
    state_ready_set: Optional[bool],
    ws_last_msg_t: float = 0.0,
    heartbeat_last_seen: Optional[float] = None,
    pool=None,
    session_date=None,
    strategy_id: Optional[str] = None,
) -> None:
    """Log a one-shot DEBUG dump describing why `ticker`'s mark resolved to 0/None.

    Safe to call from any readout path. Never raises — diagnostic only.
    """
    key = (where, ticker)
    if key in _DUMPED:
        return
    _DUMPED.add(key)

    try:
        ss = signal_state
        sf_len = len(getattr(ss, "_sf_prices", []) or []) if ss is not None else None
        last_bid = getattr(ss, "last_bid", None) if ss is not None else None
        last_ask = getattr(ss, "last_ask", None) if ss is not None else None
        in_position = getattr(ss, "_in_position", None) if ss is not None else None
        last_price = getattr(ss, "last_price", None) if ss is not None else None

        # Optional sessions-table fields (degraded_mode, cold_start_n).
        degraded_mode = None
        cold_start_n = None
        if pool is not None and session_date is not None and strategy_id is not None:
            try:
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        """
                        SELECT degraded_mode, cold_start_n
                        FROM sessions
                        WHERE strategy_id=$1 AND ticker=$2 AND session_date=$3
                        """,
                        strategy_id, ticker, session_date,
                    )
                if row is not None:
                    degraded_mode = row["degraded_mode"]
                    cold_start_n = row["cold_start_n"]
            except Exception:
                log.debug("ZERO_MARK dump: sessions query failed for %s", ticker, exc_info=True)

        # market_status import is lazy (avoids any import-time coupling).
        try:
            from live.feed import market_status
            tradable = market_status.is_tradable_now()
        except Exception:
            tradable = None

        log.warning(
            "ZERO_MARK[%s] %s: last_price=%r sf_prices_len=%r last_bid=%r last_ask=%r "
            "in_position=%r in_universe=%r state_ready=%r degraded_mode=%r cold_start_n=%r "
            "ws_age=%.1fs heartbeat_age=%s tradable_now=%r",
            where, ticker, last_price, sf_len, last_bid, last_ask,
            in_position, in_universe, state_ready_set, degraded_mode, cold_start_n,
            (_age_s(ws_last_msg_t) or -1.0),
            (f"{_age_s(heartbeat_last_seen):.1f}s" if heartbeat_last_seen else "n/a"),
            tradable,
        )
    except Exception:
        log.debug("ZERO_MARK dump failed for %s", ticker, exc_info=True)
