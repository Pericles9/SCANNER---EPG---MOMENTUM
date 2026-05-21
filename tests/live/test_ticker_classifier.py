"""Tests for live.ticker_classifier — cache, classification, and eligibility logic.

All async functions are tested via asyncio.run(). The module-level cache and pending
set are cleared before each test to prevent state leakage.
"""
from __future__ import annotations

import asyncio
import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import live.ticker_classifier as tc
from live.ticker_classifier import TickerMeta, classify_ticker


def setup_function(function):
    """Clear module-level cache and pending set before each test."""
    tc._cache.clear()
    tc._pending.clear()


# ── TickerMeta.is_eligible ────────────────────────────────────────────────────

def test_eligible_cs_xnys():
    meta = TickerMeta(ticker="X", asset_type="CS", primary_exchange="XNYS")
    assert meta.is_eligible is True


def test_eligible_cs_xnas():
    meta = TickerMeta(ticker="X", asset_type="CS", primary_exchange="XNAS")
    assert meta.is_eligible is True


def test_ineligible_etf():
    meta = TickerMeta(ticker="SPY", asset_type="ETF", primary_exchange="XNYS")
    assert meta.is_eligible is False


def test_ineligible_wrong_exchange():
    # CS on ARCX (NYSE Arca) — not in eligible set
    meta = TickerMeta(ticker="X", asset_type="CS", primary_exchange="ARCX")
    assert meta.is_eligible is False


def test_ineligible_warrant():
    meta = TickerMeta(ticker="X", asset_type="WARRANT", primary_exchange="XNAS")
    assert meta.is_eligible is False


# ── classify_ticker: cache behavior ──────────────────────────────────────────

def test_classify_ticker_cache_hit_eligible():
    """Pre-seeded eligible ticker returns from cache without any API call."""
    tc._cache["AAPL"] = TickerMeta("AAPL", "CS", "XNAS")

    async def run():
        result = await classify_ticker("AAPL", "test_key")
        assert result is not None
        assert result.is_eligible is True

    with patch("live.ticker_classifier._fetch_meta") as mock_fetch:
        asyncio.run(run())
        mock_fetch.assert_not_called()


def test_classify_ticker_cached_none_no_retry():
    """Ticker cached as None (prior failure) is returned immediately, no retry."""
    tc._cache["FAIL"] = None

    async def run():
        result = await classify_ticker("FAIL", "test_key")
        assert result is None

    with patch("live.ticker_classifier._fetch_meta") as mock_fetch:
        asyncio.run(run())
        mock_fetch.assert_not_called()


def test_classify_ticker_fetches_and_caches():
    """Cache miss triggers _fetch_meta; result is written to cache."""
    expected = TickerMeta("TSLA", "CS", "XNAS")

    async def fake_fetch(ticker, api_key):
        return expected

    async def run():
        with patch("live.ticker_classifier._fetch_meta", side_effect=fake_fetch):
            result = await classify_ticker("TSLA", "key")
        assert result == expected
        assert tc._cache["TSLA"] == expected

    asyncio.run(run())


def test_classify_ticker_404_returns_none():
    """404 from Polygon → None returned and cached."""
    async def fake_fetch(ticker, api_key):
        return None

    async def run():
        with patch("live.ticker_classifier._fetch_meta", side_effect=fake_fetch):
            result = await classify_ticker("DEAD", "key")
        assert result is None
        assert "DEAD" in tc._cache
        assert tc._cache["DEAD"] is None

    asyncio.run(run())


def test_classify_ticker_network_error_returns_none():
    """Network error during fetch → None returned and cached; does not raise."""
    async def fake_fetch(ticker, api_key):
        raise aiohttp.ClientError("connection refused")

    import aiohttp

    async def run():
        with patch("live.ticker_classifier._fetch_meta", side_effect=fake_fetch):
            result = await classify_ticker("ERR", "key")
        assert result is None
        assert tc._cache.get("ERR") is None

    asyncio.run(run())


def test_classify_ticker_pending_dedup():
    """If ticker is already in _pending, returns None without fetching."""
    tc._pending.add("RACING")

    async def run():
        result = await classify_ticker("RACING", "key")
        assert result is None
        # ticker should NOT be in cache (no fetch happened)
        assert "RACING" not in tc._cache

    with patch("live.ticker_classifier._fetch_meta") as mock_fetch:
        asyncio.run(run())
        mock_fetch.assert_not_called()


# ── CSV pre-loading ───────────────────────────────────────────────────────────

def test_load_csv_standard_columns(tmp_path):
    """CSV with standard column names populates cache correctly."""
    csv_content = "ticker,type,primary_exchange\nAAPL,CS,XNAS\nSPY,ETF,XNYS\n"
    csv_file = tmp_path / "test.csv"
    csv_file.write_text(csv_content, encoding="utf-8")

    with patch.object(tc, "_CSV_PATH", csv_file):
        tc._cache.clear()
        tc._load_csv()

    assert "AAPL" in tc._cache
    assert tc._cache["AAPL"].asset_type == "CS"
    assert tc._cache["AAPL"].primary_exchange == "XNAS"
    assert tc._cache["AAPL"].is_eligible is True

    assert "SPY" in tc._cache
    assert tc._cache["SPY"].asset_type == "ETF"
    assert tc._cache["SPY"].is_eligible is False


def test_load_csv_missing_file_is_silent(tmp_path):
    """Missing CSV file does not raise and leaves cache empty."""
    missing = tmp_path / "nonexistent.csv"
    with patch.object(tc, "_CSV_PATH", missing):
        tc._cache.clear()
        tc._load_csv()

    assert len(tc._cache) == 0
