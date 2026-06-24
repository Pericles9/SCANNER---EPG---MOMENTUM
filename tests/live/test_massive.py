"""Task 1 — Massive REST price helper tests (parsing + graceful failure)."""
from __future__ import annotations

import asyncio

import pytest

from live.feed.massive import _parse_snapshot, fetch_mark, resolve_mark


def test_parse_full_snapshot():
    body = {"ticker": {"lastQuote": {"p": 9.5, "P": 10.5}, "lastTrade": {"p": 10.0}}}
    assert _parse_snapshot(body) == (9.5, 10.5, 10.0)


def test_parse_missing_quote_keeps_last():
    body = {"ticker": {"lastTrade": {"p": 10.0}}}
    assert _parse_snapshot(body) == (None, None, 10.0)


def test_parse_zero_and_negative_become_none():
    body = {"ticker": {"lastQuote": {"p": 0.0, "P": -1.0}, "lastTrade": {"p": 0}}}
    assert _parse_snapshot(body) == (None, None, None)


def test_parse_empty_body():
    assert _parse_snapshot({}) == (None, None, None)
    assert _parse_snapshot(None) == (None, None, None)


def test_fetch_mark_no_api_key_returns_none(monkeypatch):
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    bid, ask, last = asyncio.run(fetch_mark("ABCD", api_key=None))
    assert (bid, ask, last) == (None, None, None)


# ── resolve_mark: prefer live signal_state, fall back to Massive REST ──────────

class _SS:
    def __init__(self, result):
        self._result = result

    def mark(self, now_ns, stale_s=5.0):
        return self._result


def test_resolve_mark_prefers_signal_state(monkeypatch):
    async def _no_fetch(*a, **k):
        raise AssertionError("fetch_mark must not be called when signal_state is fresh")
    monkeypatch.setattr("live.feed.massive.fetch_mark", _no_fetch)
    price, src, age = asyncio.run(resolve_mark(_SS((12.0, "LIVE", 0.0)), "X", 0))
    assert (price, src) == (12.0, "LIVE")


def test_resolve_mark_falls_back_to_rest_last(monkeypatch):
    async def _fm(ticker, api_key=None, **k):
        return (None, None, 9.5)
    monkeypatch.setattr("live.feed.massive.fetch_mark", _fm)
    price, src, age = asyncio.run(resolve_mark(_SS((None, "NONE", 0.0)), "X", 0))
    assert (price, src) == (9.5, "REST")


def test_resolve_mark_no_signal_state_uses_rest_mid(monkeypatch):
    async def _fm(ticker, api_key=None, **k):
        return (9.0, 9.2, None)
    monkeypatch.setattr("live.feed.massive.fetch_mark", _fm)
    price, src, age = asyncio.run(resolve_mark(None, "X", 0))   # orphaned position, no ctx
    assert src == "REST"
    assert price == pytest.approx(9.1)


def test_resolve_mark_none_when_nothing(monkeypatch):
    async def _fm(ticker, api_key=None, **k):
        return (None, None, None)
    monkeypatch.setattr("live.feed.massive.fetch_mark", _fm)
    price, src, age = asyncio.run(resolve_mark(None, "X", 0))
    assert price is None and src == "NONE"
