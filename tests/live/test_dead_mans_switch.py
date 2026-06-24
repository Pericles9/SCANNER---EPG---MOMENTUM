"""Phase 3 — halt-aware dead-man's-switch tests.

A real halt produces the same per-ticker tick gap as feed death; the switch must
NOT force-exit a halted (or halt-suspected) ticker, but must still flatten on true
symbol-level feed death and escalate once if a halt runs past the hard cap.

Exercises the extracted single-pass helper `_dead_mans_switch_pass`. Offline.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from live.feed.signal_loop import _dead_mans_switch_pass
from live.orders.risk import FlattenTickerRequest

_DEAD_MAN_S = 30.0
_MAX_HALT_S = 1800.0


def _cfg():
    cfg = MagicMock()
    cfg.risk.dead_man_timeout_s = _DEAD_MAN_S
    cfg.luld.max_halt_hold_s = _MAX_HALT_S
    return cfg


def _ss(halted=False, luld_last_seen=0.0):
    return SimpleNamespace(
        is_halted=lambda: halted,
        luld_last_seen=luld_last_seen,
    )


def _universe(ticker, ss):
    return {ticker: SimpleNamespace(signal_state=ss)}


def _risk(positions):
    risk = MagicMock()
    risk.has_position.side_effect = lambda t: t in positions
    return risk


def _run_pass(ticker, ss, *, now=1000.0, ws_healthy=True, ws_age=1.0,
              halt_since=None, alerted=None, escalated=None, has_pos=True):
    q = MagicMock()
    halt_since = halt_since if halt_since is not None else {}
    alerted = alerted if alerted is not None else set()
    escalated = escalated if escalated is not None else set()
    positions = {ticker} if has_pos else set()
    with patch("live.feed.signal_loop.CFG", _cfg()):
        _dead_mans_switch_pass(
            stale={ticker}, universe=_universe(ticker, ss), risk_state=_risk(positions),
            order_queue=q, now=now, ws_healthy=ws_healthy, ws_age=ws_age,
            halt_since=halt_since, alerted=alerted, escalated=escalated, telegram=None,
        )
    return q, halt_since, alerted, escalated


def _flatten_calls(q):
    return [c.args[0] for c in q.put_nowait.call_args_list
            if isinstance(c.args[0], FlattenTickerRequest)]


# ── Core disambiguation ───────────────────────────────────────────────────────

def test_halted_ticker_not_flattened():
    q, *_ = _run_pass("ABCD", _ss(halted=True, luld_last_seen=999.5), now=1000.0)
    assert _flatten_calls(q) == []


def test_halt_suspected_luld_fresh_not_flattened():
    """No 17/18 (NYSE/AMEX) but LULD bands still arriving → halt-suspected → hold."""
    q, *_ = _run_pass("ABCD", _ss(halted=False, luld_last_seen=995.0), now=1000.0)
    assert _flatten_calls(q) == []


def test_true_feed_death_flattens():
    """No halt, no recent LULD, global WS healthy → symbol feed death → flatten."""
    q, *_ = _run_pass("ABCD", _ss(halted=False, luld_last_seen=0.0),
                      now=1000.0, ws_healthy=True)
    calls = _flatten_calls(q)
    assert len(calls) == 1
    assert calls[0].ticker == "ABCD"
    assert calls[0].reason == "dead_mans_switch"


def test_feed_death_with_ws_down_defers():
    """Global WS also stale → defer to the WS-disconnect path, do not flatten here."""
    q, *_ = _run_pass("ABCD", _ss(halted=False, luld_last_seen=0.0),
                      now=1000.0, ws_healthy=False, ws_age=120.0)
    assert _flatten_calls(q) == []


def test_no_position_skipped():
    q, *_ = _run_pass("ABCD", _ss(halted=True), has_pos=False)
    assert _flatten_calls(q) == []


# ── Hard cap escalation ───────────────────────────────────────────────────────

def test_halt_cap_escalates_exactly_once():
    ss = _ss(halted=True, luld_last_seen=1e9)   # luld_fresh kept true across calls
    halt_since: dict = {}
    alerted: set = set()
    escalated: set = set()

    # Pass 1: hold begins, held=0 → alert, no flatten.
    q1, *_ = _run_pass("ABCD", ss, now=1000.0,
                       halt_since=halt_since, alerted=alerted, escalated=escalated)
    assert _flatten_calls(q1) == []
    assert halt_since["ABCD"] == 1000.0

    # Pass 2: held exceeds cap → escalate one FlattenTicker.
    q2, *_ = _run_pass("ABCD", ss, now=1000.0 + _MAX_HALT_S + 1,
                       halt_since=halt_since, alerted=alerted, escalated=escalated)
    calls2 = _flatten_calls(q2)
    assert len(calls2) == 1
    assert calls2[0].reason == "halt_hold_cap_exceeded"

    # Pass 3: still halted past cap → already escalated → no repeat flatten.
    q3, *_ = _run_pass("ABCD", ss, now=1000.0 + _MAX_HALT_S + 10,
                       halt_since=halt_since, alerted=alerted, escalated=escalated)
    assert _flatten_calls(q3) == []


def test_recovery_clears_bookkeeping():
    """A ticker that recovers (no longer stale) has its halt bookkeeping forgotten."""
    halt_since = {"ABCD": 500.0}
    alerted = {"ABCD"}
    escalated = {"ABCD"}
    q = MagicMock()
    with patch("live.feed.signal_loop.CFG", _cfg()):
        _dead_mans_switch_pass(
            stale=set(), universe={}, risk_state=_risk(set()),
            order_queue=q, now=2000.0, ws_healthy=True, ws_age=1.0,
            halt_since=halt_since, alerted=alerted, escalated=escalated, telegram=None,
        )
    assert "ABCD" not in halt_since
    assert "ABCD" not in alerted
    assert "ABCD" not in escalated
