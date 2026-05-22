"""Formatting helpers for bot command responses."""
from __future__ import annotations

import time
from typing import Optional


def _hold_str(hold_sec: Optional[float]) -> str:
    if hold_sec is None:
        return "—"
    m, s = divmod(int(hold_sec), 60)
    return f"{m}m{s:02d}s"


def _age_str(last_t: float) -> str:
    """Human-readable age from a monotonic timestamp (0.0 = never)."""
    if last_t == 0.0:
        return "never"
    age = time.monotonic() - last_t
    if age < 60:
        return f"{age:.0f}s ago"
    return f"{age/60:.1f}m ago"


def format_trade_row(trade: dict) -> str:
    ticker = trade.get("ticker", "?")
    bucket = (trade.get("session_bucket") or "?")[:3].upper()
    entry = trade.get("entry_price") or 0.0
    exit_ = trade.get("exit_price") or 0.0
    pnl_d = trade.get("pnl_dollar") or 0.0
    pnl_p = (trade.get("pnl_pct") or 0.0) * 100
    hold = _hold_str(trade.get("hold_sec"))
    reason = (trade.get("exit_reason") or "?")[:10]
    sign = "+" if pnl_d >= 0 else ""
    return (
        f"{ticker:<6} {bucket:<3} "
        f"{entry:.2f}→{exit_:.2f} "
        f"{sign}{pnl_d:.2f}({sign}{pnl_p:.1f}%) "
        f"{hold} {reason}"
    )


def format_universe_row(
    ticker: str,
    quartile: Optional[int],
    rank: Optional[int],
    n: Optional[int],
    pct_change: float,
    state: str,
) -> str:
    q_str = f"Q{quartile}" if quartile else "Q?"
    rank_str = f"{rank}/{n}" if rank and n else "?/?"
    return f"{ticker:<6} {q_str} {rank_str:<6} {pct_change:+.1f}% MDR✗ {state}"


def format_services_row(name: str, ok: bool, detail: str) -> str:
    mark = "✓" if ok else "✗"
    return f"{mark} {name:<22} {detail}"


def format_position_block(
    ticker: str,
    avg_cost: float,
    qty: int,
    entry_ns: Optional[int],
    current_price: float,
    epg_gate: str,
    lambda_hat: float,
    lambda_ref: float,
    scanner_context: dict,
) -> str:
    unreal = (current_price - avg_cost) * qty
    unreal_pct = (current_price - avg_cost) / avg_cost * 100 if avg_cost > 0 else 0.0
    sign = "+" if unreal >= 0 else ""

    hold_str = "?"
    if entry_ns:
        hold_sec = (time.time_ns() - entry_ns) / 1e9
        hold_str = _hold_str(hold_sec)

    lv_ratio = f"{lambda_hat:.4f} / {lambda_ref:.4f}" if lambda_ref > 0 else "n/a"
    quartile = scanner_context.get("scanner_quartile", "?")
    rank = scanner_context.get("scanner_rank", "?")
    n_total = scanner_context.get("scanner_n", "?")
    pct = scanner_context.get("pct_change", 0.0)

    lines = [
        f"POSITION: {ticker}",
        f"  Entry: ${avg_cost:.2f} × {qty} shares",
        f"  Current: ${current_price:.2f}",
        f"  Unrealised: {sign}${unreal:.2f} ({sign}{unreal_pct:.2f}%)",
        f"  Hold: {hold_str}",
        f"  EPG gate: {epg_gate}",
        f"  λ_v / λ_ref: {lv_ratio}",
        f"  Scanner: Q{quartile} rank={rank}/{n_total} gap={pct:+.1f}%",
    ]
    return "\n".join(lines)
