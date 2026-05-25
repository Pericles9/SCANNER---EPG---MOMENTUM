"""Telegram command handlers. Each handler reads BotState — never writes."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes

from live.bot.auth import authorised_only
from live.bot.formatters import (
    _age_str,
    _hold_str,
    format_position_block,
    format_services_row,
    format_trade_row,
    format_universe_row,
)
from live.bot.probes import run_all_probes
from live.bot.ratelimit import is_debounced

_ET = ZoneInfo("America/New_York")


def _bot_state(context: ContextTypes.DEFAULT_TYPE):
    return context.bot_data["state"]


@authorised_only
async def universe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_debounced("universe"):
        return
    state = _bot_state(context)

    lines = ["*UNIVERSE*"]
    rows = []
    for ticker, ctx in list(state.universe.items()):
        sc = ctx.scanner_context or {}
        if state.risk_state.has_position(ticker):
            ticker_state = "IN_POSITION"
        elif ctx.state_ready.is_set():
            ticker_state = "WATCHING"
        else:
            ticker_state = "WARMING_UP"

        rows.append((
            sc.get("scanner_rank", 9999),
            format_universe_row(
                ticker=ticker,
                quartile=sc.get("scanner_quartile"),
                rank=sc.get("scanner_rank"),
                n=sc.get("scanner_n"),
                pct_change=sc.get("pct_change", 0.0),
                state=ticker_state,
            ),
        ))

    rows.sort(key=lambda x: x[0])
    for _, row in rows:
        lines.append(row)

    if state.closed_today:
        lines.append("")
        lines.append(f"Closed today: {', '.join(sorted(state.closed_today))}")

    lines.append(f"\nActive: {len(state.universe)}  Closed: {len(state.closed_today)}")
    await update.message.reply_text("\n".join(lines))


@authorised_only
async def trades(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _bot_state(context)
    today = datetime.now(_ET).date()

    try:
        async with state.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ticker, session_bucket, entry_price, exit_price, qty,
                       pnl_dollar, pnl_pct, hold_sec, exit_reason, entry_ns
                FROM trades
                WHERE strategy_id=$1 AND session_date=$2
                ORDER BY entry_ns ASC
                """,
                "epg_v1",
                today,
            )
    except Exception as exc:
        await update.message.reply_text(f"DB error: {exc}")
        return

    if not rows:
        await update.message.reply_text("No completed trades today.")
        return

    total_pnl = sum(r["pnl_dollar"] or 0.0 for r in rows)
    wins = sum(1 for r in rows if (r["pnl_dollar"] or 0.0) > 0)
    win_rate = wins / len(rows) * 100 if rows else 0.0
    sign = "+" if total_pnl >= 0 else ""

    lines = [f"*TRADES — {today}*"]
    for r in rows:
        lines.append(format_trade_row(dict(r)))

    lines.append("")
    lines.append(
        f"Count: {len(rows)}  Net: {sign}${total_pnl:.2f}  Win rate: {win_rate:.0f}%"
    )
    await update.message.reply_text("\n".join(lines))


@authorised_only
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_debounced("status"):
        return
    state = _bot_state(context)
    now_et = datetime.now(_ET)
    today = now_et.date()

    from backtest.runner import session_bucket as _session_bucket
    import time as _time

    t_sec = (now_et.hour * 3600 + now_et.minute * 60 + now_et.second)
    bkt = _session_bucket(float(t_sec))

    open_pos = state.risk_state.open_positions
    pos_line = "None"
    if open_pos:
        ticker = next(iter(open_pos))
        pos = open_pos[ticker]
        ctx = state.universe.get(ticker)
        if ctx and ctx.signal_state:
            cur_price = ctx.signal_state.last_price
            unreal = (cur_price - pos["avg_cost"]) * pos["qty"]
            sign = "+" if unreal >= 0 else ""
            pos_line = f"{ticker} {pos['qty']}sh @ ${pos['avg_cost']:.2f} → ${cur_price:.2f} ({sign}${unreal:.2f})"
        else:
            pos_line = f"{ticker} {pos['qty']}sh @ ${pos['avg_cost']:.2f}"

    daily_pnl = state.risk_state.daily_pnl
    limit = state.risk_state.max_daily_loss
    runway = daily_pnl - limit
    pnl_sign = "+" if daily_pnl >= 0 else ""

    poll_age = _age_str(state.scanner_last_poll_t[0])
    ws_age = _age_str(state.ws_last_msg_t[0])

    hb_vals = list(state.heartbeat._last_seen.values())
    p2_age = _age_str(max(hb_vals)) if hb_vals else "no tickers"

    worker_age = _age_str(state.worker_last_wake_t[0])

    lines = [
        f"*STATUS — {now_et.strftime('%H:%M:%S ET')}*",
        f"Session: {bkt}  Date: {today}",
        "",
        f"Position: {pos_line}",
        "",
        f"Daily PnL: {pnl_sign}${daily_pnl:.2f}  Runway: ${runway:.2f}",
        f"Universe: {len(state.universe)} active  Closed: {len(state.closed_today)}",
        f"Open positions: {len(open_pos)}/{state.risk_state.max_concurrent}",
        "",
        f"Scanner poll: {poll_age}",
        f"Polygon WS: {ws_age}",
        f"Signal loop: {p2_age}",
        f"Order worker: {worker_age}",
    ]
    await update.message.reply_text("\n".join(lines))


@authorised_only
async def services(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_debounced("services"):
        return
    state = _bot_state(context)
    now_et = datetime.now(_ET)

    probe_results = await asyncio.wait_for(
        run_all_probes(state.pool, state.ibkr, state.polygon_api_key),
        timeout=3.0,
    )

    import time as _time

    poll_age = _age_str(state.scanner_last_poll_t[0])
    ws_age = _age_str(state.ws_last_msg_t[0])
    hb_vals = list(state.heartbeat._last_seen.values())
    p2_age = _age_str(max(hb_vals)) if hb_vals else "never"
    worker_age = _age_str(state.worker_last_wake_t[0])

    lines = [f"*SERVICES — {now_et.strftime('%H:%M:%S ET')}*"]
    for name, ok, detail in probe_results:
        lines.append(format_services_row(name, ok, detail))

    lines.append("")
    lines.append(f"{'✓' if state.scanner_last_poll_t[0] > 0 else '?'} Process 1 (scanner)   {poll_age}")
    lines.append(f"{'✓' if hb_vals else '?'} Process 2 (feed)      {p2_age}")
    lines.append(f"{'✓' if state.worker_last_wake_t[0] > 0 else '?'} Process 3 (worker)    {worker_age}")
    lines.append(f"{'✓' if state.ws_last_msg_t[0] > 0 else '?'} Polygon WS            {ws_age}")

    all_ok = all(ok for _, ok, _ in probe_results)
    lines.append("")
    lines.append("All systems OK" if all_ok else "DEGRADED — see above")
    await update.message.reply_text("\n".join(lines))


@authorised_only
async def position(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _bot_state(context)
    open_pos = state.risk_state.open_positions

    if not open_pos:
        await update.message.reply_text("No open position.")
        return

    ticker = next(iter(open_pos))
    pos = open_pos[ticker]
    ctx = state.universe.get(ticker)

    cur_price = 0.0
    epg_gate = "?"
    lambda_hat = 0.0
    lambda_ref = 0.0
    sc = {}
    if ctx and ctx.signal_state:
        ss = ctx.signal_state
        cur_price = ss.last_price
        epg_gate = ss.epg_gate_state
        lambda_hat = ss.last_lambda_hat
        lambda_ref = ss.last_lambda_ref
        sc = ss.scanner_context

    # Entry ns from DB is nice but adds latency; derive hold from position open if available
    block = format_position_block(
        ticker=ticker,
        avg_cost=pos["avg_cost"],
        qty=pos["qty"],
        entry_ns=None,
        current_price=cur_price,
        epg_gate=epg_gate,
        lambda_hat=lambda_hat,
        lambda_ref=lambda_ref,
        scanner_context=sc,
    )
    await update.message.reply_text(block)


@authorised_only
async def risk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _bot_state(context)
    rs = state.risk_state

    unreal_total = 0.0
    for ticker, pos in rs.open_positions.items():
        ctx = state.universe.get(ticker)
        if ctx and ctx.signal_state:
            cur = ctx.signal_state.last_price
            unreal_total += (cur - pos["avg_cost"]) * pos["qty"]

    combined = rs.daily_pnl + unreal_total
    runway = rs.daily_pnl - rs.max_daily_loss
    trading_ok = not rs._loss_limit_hit

    def _sign(v: float) -> str:
        return "+" if v >= 0 else ""

    lines = [
        "*RISK*",
        f"Realised PnL:   {_sign(rs.daily_pnl)}${rs.daily_pnl:.2f}",
        f"Unrealised PnL: {_sign(unreal_total)}${unreal_total:.2f}",
        f"Combined:       {_sign(combined)}${combined:.2f}",
        f"Daily limit:    ${rs.max_daily_loss:.2f}",
        f"Runway:         ${runway:.2f}",
        f"Open positions: {len(rs.open_positions)}/{rs.max_concurrent}",
        f"Trading:        {'OK' if trading_ok else 'BLOCKED (loss limit)'}",
    ]
    await update.message.reply_text("\n".join(lines))


@authorised_only
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "*EPG Live Bot — Commands*\n"
        "/status    Session overview (≤25 lines)\n"
        "/universe  All tracked tickers and states\n"
        "/scanner   Scanner snapshot + universe\n"
        "/position  Current open position detail\n"
        "/trades    Today's completed trades\n"
        "/risk      Risk state snapshot\n"
        "/services  Full service health probe\n"
        "/help      This message\n"
        "/kill      Kill switch — flatten all positions"
    )
    await update.message.reply_text(text)


def _format_scanner_response(state, now_et: datetime = None) -> str:
    """Pure formatter for /scanner — returns the reply text. Testable without Telegram.

    state must expose: universe (dict), closed_today (set), risk_state (with has_position),
    ws_last_msg_t (list[float] — monotonic-time mutable box).
    """
    from live import scanner_monitor as sm
    from live.feed import market_status as ms
    if now_et is None:
        now_et = datetime.now(_ET)
    time_str = now_et.strftime("%H:%M:%S ET")

    # Session-closed detection — prefer real Massive /v1/marketstatus/now state.
    # Fall back to a clock check (4am–8pm ET) if the status cache hasn't been populated yet.
    status = ms.get_last_market_status()
    holidays = ms.get_upcoming_holidays()
    today_et = now_et.date()

    if status is not None:
        if not status.is_tradable:
            holiday_name = ms.today_holiday_name(holidays, today_et)
            next_open = ms.next_open_date(holidays, today_et)
            if holiday_name:
                msg = f"📡 Scanner — closed for {holiday_name}."
            else:
                msg = f"📡 Scanner — session closed."
            if next_open is not None:
                msg += f" Next open: {next_open.strftime('%a %Y-%m-%d')} 04:00 ET."
            return msg
    else:
        # Status not yet fetched — clock fallback
        if now_et.hour < 4 or now_et.hour >= 20:
            return f"📡 Scanner — session closed. Next open 04:00 ET."

    snapshot = sm.get_last_scanner_snapshot()
    snapshot_by_ticker = {s["ticker"]: s for s in snapshot}

    universe_items = list(state.universe.items())
    if not universe_items:
        return (
            f"📡 Scanner — {time_str}\n"
            f"Universe: empty\n\n"
            f"Scanner: {len(snapshot)} names on deck"
        )

    # Enrich universe rows with latest snapshot pct_change (fall back to scanner_context)
    rows = []
    for ticker, ctx in universe_items:
        snap = snapshot_by_ticker.get(ticker)
        sc = ctx.scanner_context or {}
        if snap is not None:
            pct = snap["pct_change"]
            quartile = snap["quartile"]
            rank = snap["rank"]
        else:
            pct = sc.get("pct_change", 0.0)
            quartile = sc.get("scanner_quartile", 0)
            rank = sc.get("scanner_rank", 0)
        has_pos = state.risk_state.has_position(ticker)
        rows.append((pct, ticker, quartile, rank, has_pos))

    rows.sort(key=lambda r: -r[0])

    lines = [
        f"📡 Scanner — {time_str}",
        f"Universe: {len(rows)} active",
        "",
    ]
    for pct, ticker, quartile, rank, has_pos in rows:
        pos_tag = "  [POSITION]" if has_pos else ""
        lines.append(f"  {ticker:<6} {pct:+.0f}%  Q{quartile}  rank {rank}{pos_tag}")

    # Scanner block — snapshot names not in universe
    universe_set = set(state.universe.keys())
    not_in_universe = [s for s in snapshot if s["ticker"] not in universe_set]
    lines.append("")
    if not_in_universe:
        lines.append(f"Scanner (not in universe): {len(not_in_universe)} names")
        shown = not_in_universe[:3]
        inline = ", ".join(f"{s['ticker']} +{s['pct_change']:.0f}%" for s in shown)
        if len(not_in_universe) > 3:
            inline += f" ... (+{len(not_in_universe) - 3} more)"
        lines.append(f"  {inline}")
    else:
        lines.append("Scanner: no other names on deck")

    return "\n".join(lines)


@authorised_only
async def scanner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_debounced("scanner"):
        return
    state = _bot_state(context)
    text = _format_scanner_response(state)
    await update.message.reply_text(text)
