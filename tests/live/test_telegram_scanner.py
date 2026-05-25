"""Tests for the /scanner Telegram command — pure formatter, no Telegram client."""
from __future__ import annotations

import time
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

import live.scanner_monitor as sm
from live.bot.handlers import _format_scanner_response
from live.feed import market_status as ms

_ET = ZoneInfo("America/New_York")


def _build_state(universe: dict, positions: set, ws_age_s: float = 0.0):
    """Build a mock BotState-like object."""
    state = MagicMock()
    state.universe = universe
    state.closed_today = set()
    risk = MagicMock()
    risk.has_position = lambda t: t in positions
    state.risk_state = risk
    # ws_last_msg_t is a mutable box; recent (now) = active feed
    state.ws_last_msg_t = [time.monotonic() - ws_age_s]
    return state


def _ctx(pct: float, quartile: int, rank: int):
    ctx = MagicMock()
    ctx.scanner_context = {
        "pct_change": pct,
        "scanner_quartile": quartile,
        "scanner_rank": rank,
    }
    return ctx


def _fixed_now() -> datetime:
    return datetime(2026, 5, 25, 14, 32, 5, tzinfo=_ET)


# ── Core formatter behaviour ──────────────────────────────────────────────────

def test_scanner_response_includes_universe_tickers():
    sm.last_scanner_snapshot = [
        {"ticker": "MSTR", "pct_change": 187.0, "quartile": 2, "rank": 1, "n": 7},
        {"ticker": "SOUN", "pct_change": 61.0, "quartile": 4, "rank": 3, "n": 7},
    ]
    state = _build_state(
        universe={"MSTR": _ctx(187.0, 2, 1), "SOUN": _ctx(61.0, 4, 3)},
        positions=set(),
    )

    text = _format_scanner_response(state, now_et=_fixed_now())

    assert "MSTR" in text
    assert "SOUN" in text
    assert "Universe: 2 active" in text


def test_scanner_response_marks_position_tag():
    sm.last_scanner_snapshot = [
        {"ticker": "MSTR", "pct_change": 187.0, "quartile": 2, "rank": 1, "n": 5},
        {"ticker": "SOUN", "pct_change": 61.0, "quartile": 4, "rank": 3, "n": 5},
    ]
    state = _build_state(
        universe={"MSTR": _ctx(187.0, 2, 1), "SOUN": _ctx(61.0, 4, 3)},
        positions={"MSTR"},
    )

    text = _format_scanner_response(state, now_et=_fixed_now())

    # MSTR has [POSITION] tag, SOUN does not
    lines = text.split("\n")
    mstr_line = next(line for line in lines if "MSTR" in line)
    soun_line = next(line for line in lines if "SOUN" in line)
    assert "[POSITION]" in mstr_line
    assert "[POSITION]" not in soun_line


def test_scanner_response_shows_plus_n_more():
    """5 scanner names not in universe → show 3, append '(+2 more)'."""
    sm.last_scanner_snapshot = [
        {"ticker": "MSTR", "pct_change": 187.0, "quartile": 2, "rank": 1, "n": 7},
        {"ticker": "SOUN", "pct_change": 61.0, "quartile": 4, "rank": 3, "n": 7},
        {"ticker": "PROP", "pct_change": 38.0, "quartile": 3, "rank": 5, "n": 7},
        {"ticker": "VERB", "pct_change": 35.0, "quartile": 3, "rank": 6, "n": 7},
        {"ticker": "RCAT", "pct_change": 33.0, "quartile": 4, "rank": 7, "n": 7},
        {"ticker": "ABCD", "pct_change": 31.0, "quartile": 4, "rank": 8, "n": 7},
        {"ticker": "EFGH", "pct_change": 30.5, "quartile": 4, "rank": 9, "n": 7},
    ]
    state = _build_state(
        universe={"MSTR": _ctx(187.0, 2, 1), "SOUN": _ctx(61.0, 4, 3)},
        positions={"MSTR"},
    )

    text = _format_scanner_response(state, now_et=_fixed_now())

    assert "(+2 more)" in text
    # First three not-in-universe names are shown inline
    assert "PROP" in text
    assert "VERB" in text
    assert "RCAT" in text


def test_scanner_response_sorts_universe_by_pct_desc():
    sm.last_scanner_snapshot = [
        {"ticker": "MSTR", "pct_change": 187.0, "quartile": 2, "rank": 1, "n": 3},
        {"ticker": "SOUN", "pct_change": 61.0, "quartile": 4, "rank": 3, "n": 3},
        {"ticker": "INDO", "pct_change": 44.0, "quartile": 3, "rank": 5, "n": 3},
    ]
    state = _build_state(
        universe={
            "INDO": _ctx(44.0, 3, 5),  # lowest
            "MSTR": _ctx(187.0, 2, 1),  # highest
            "SOUN": _ctx(61.0, 4, 3),
        },
        positions=set(),
    )

    text = _format_scanner_response(state, now_et=_fixed_now())
    lines = text.split("\n")

    # Find ticker lines in the universe block (indented with two spaces)
    ticker_lines = [
        line for line in lines
        if line.startswith("  ") and any(t in line for t in ("MSTR", "SOUN", "INDO"))
    ]
    # Order should be MSTR, SOUN, INDO (descending pct)
    assert ticker_lines[0].lstrip().startswith("MSTR")
    assert ticker_lines[1].lstrip().startswith("SOUN")
    assert ticker_lines[2].lstrip().startswith("INDO")


# ── Edge cases ────────────────────────────────────────────────────────────────

def test_empty_universe_response():
    """Universe empty but scanner has names → 'Universe: empty' + 'N names on deck'."""
    sm.last_scanner_snapshot = [
        {"ticker": "PROP", "pct_change": 38.0, "quartile": 3, "rank": 1, "n": 2},
        {"ticker": "VERB", "pct_change": 35.0, "quartile": 3, "rank": 2, "n": 2},
    ]
    state = _build_state(universe={}, positions=set())

    text = _format_scanner_response(state, now_et=_fixed_now())

    assert "Universe: empty" in text
    assert "2 names on deck" in text


def test_session_closed_response_at_overnight_hour():
    """Outside trading hours (20:00–04:00 ET) → session-closed message."""
    sm.last_scanner_snapshot = []
    state = _build_state(universe={}, positions=set())
    overnight = datetime(2026, 5, 25, 22, 0, 0, tzinfo=_ET)  # 22:00 ET

    text = _format_scanner_response(state, now_et=overnight)

    assert "session closed" in text.lower()
    assert "04:00 ET" in text


def test_session_closed_response_at_early_morning():
    """03:00 ET (before 04:00 ET pre-market open) → session-closed message."""
    sm.last_scanner_snapshot = []
    state = _build_state(universe={}, positions=set())
    early = datetime(2026, 5, 25, 3, 0, 0, tzinfo=_ET)

    text = _format_scanner_response(state, now_et=early)

    assert "session closed" in text.lower()


def test_no_session_closed_at_pre_market_with_empty_universe():
    """06:15 ET with empty universe is NORMAL pre-market — must NOT show session closed."""
    sm.last_scanner_snapshot = []
    state = _build_state(universe={}, positions=set())
    pre_market = datetime(2026, 5, 25, 6, 15, 0, tzinfo=_ET)

    text = _format_scanner_response(state, now_et=pre_market)

    assert "session closed" not in text.lower()
    assert "Universe: empty" in text


def test_no_session_closed_when_universe_active():
    """Active universe during trading hours → normal output."""
    sm.last_scanner_snapshot = [
        {"ticker": "MSTR", "pct_change": 187.0, "quartile": 2, "rank": 1, "n": 1},
    ]
    state = _build_state(
        universe={"MSTR": _ctx(187.0, 2, 1)},
        positions=set(),
    )

    text = _format_scanner_response(state, now_et=_fixed_now())

    assert "session closed" not in text.lower()
    assert "MSTR" in text


def teardown_module(module):
    """Reset module-level state so tests don't leak."""
    sm.last_scanner_snapshot = []
    ms.last_market_status = None
    ms.upcoming_holidays = []


# ── Real market-status-driven session-closed messages ────────────────────────

def _mock_status(market: str, early: bool = False, after: bool = False) -> ms.MarketStatus:
    return ms.MarketStatus(
        market=market, early_hours=early, after_hours=after,
        server_time="", nyse=market, nasdaq=market,
    )


def test_session_closed_with_holiday_name(monkeypatch):
    """Memorial Day: closed + today matches NYSE holiday → response names the holiday."""
    monkeypatch.setattr(ms, "last_market_status", _mock_status("closed"))
    monkeypatch.setattr(ms, "upcoming_holidays", [
        ms.Holiday(date="2026-05-25", exchange="NYSE", name="Memorial Day", status="closed"),
        ms.Holiday(date="2026-05-25", exchange="NASDAQ", name="Memorial Day", status="closed"),
    ])
    sm.last_scanner_snapshot = []
    state = _build_state(universe={}, positions=set())
    today_memorial = datetime(2026, 5, 25, 10, 0, 0, tzinfo=_ET)

    text = _format_scanner_response(state, now_et=today_memorial)

    assert "Memorial Day" in text
    assert "closed" in text.lower()
    # Next open: Tue 2026-05-26
    assert "2026-05-26" in text


def test_session_closed_weekend_no_holiday(monkeypatch):
    """Saturday: closed but not a holiday → generic 'session closed' + Monday open."""
    monkeypatch.setattr(ms, "last_market_status", _mock_status("closed"))
    monkeypatch.setattr(ms, "upcoming_holidays", [])
    sm.last_scanner_snapshot = []
    state = _build_state(universe={}, positions=set())
    saturday = datetime(2026, 5, 23, 10, 0, 0, tzinfo=_ET)

    text = _format_scanner_response(state, now_et=saturday)

    assert "session closed" in text.lower()
    # Monday 2026-05-25 is Memorial Day in real life, but with empty holidays here,
    # next_open is just the next weekday (Mon 2026-05-25)
    assert "2026-05-25" in text


def test_pre_market_with_real_status_shows_normal_output(monkeypatch):
    """At 06:15 ET with real status reporting earlyHours=True → normal output, not closed."""
    monkeypatch.setattr(ms, "last_market_status", _mock_status("extended-hours", early=True))
    monkeypatch.setattr(ms, "upcoming_holidays", [])
    sm.last_scanner_snapshot = []
    state = _build_state(universe={}, positions=set())
    pre_market = datetime(2026, 5, 26, 6, 15, 0, tzinfo=_ET)

    text = _format_scanner_response(state, now_et=pre_market)

    assert "closed" not in text.lower()
    assert "Universe: empty" in text


def test_open_market_normal_output(monkeypatch):
    """Regular hours, status='open' → normal scanner output."""
    monkeypatch.setattr(ms, "last_market_status", _mock_status("open"))
    monkeypatch.setattr(ms, "upcoming_holidays", [])
    sm.last_scanner_snapshot = [
        {"ticker": "MSTR", "pct_change": 187.0, "quartile": 2, "rank": 1, "n": 1},
    ]
    state = _build_state(
        universe={"MSTR": _ctx(187.0, 2, 1)},
        positions=set(),
    )
    rth = datetime(2026, 5, 26, 11, 0, 0, tzinfo=_ET)

    text = _format_scanner_response(state, now_et=rth)

    assert "MSTR" in text
    assert "closed" not in text.lower()


def test_falls_back_to_clock_when_status_unavailable(monkeypatch):
    """If market_status hasn't loaded yet, fall back to clock-based check."""
    monkeypatch.setattr(ms, "last_market_status", None)
    sm.last_scanner_snapshot = []
    state = _build_state(universe={}, positions=set())
    overnight = datetime(2026, 5, 26, 22, 0, 0, tzinfo=_ET)

    text = _format_scanner_response(state, now_et=overnight)

    assert "session closed" in text.lower()


def test_fallback_does_not_block_normal_pre_market(monkeypatch):
    """Status unavailable + 06:15 ET (between 04:00–20:00) → normal output, not closed."""
    monkeypatch.setattr(ms, "last_market_status", None)
    sm.last_scanner_snapshot = []
    state = _build_state(universe={}, positions=set())
    pre_market = datetime(2026, 5, 26, 6, 15, 0, tzinfo=_ET)

    text = _format_scanner_response(state, now_et=pre_market)

    assert "session closed" not in text.lower()
    assert "Universe: empty" in text
