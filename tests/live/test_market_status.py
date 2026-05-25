"""Tests for live.feed.market_status — pure helpers + live endpoint smoke test."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import date

import aiohttp
import pytest

from live.feed import market_status as ms


# ── Pure helpers ──────────────────────────────────────────────────────────────

def test_market_status_is_tradable_open():
    s = ms.MarketStatus(
        market="open", early_hours=False, after_hours=False,
        server_time="", nyse="open", nasdaq="open",
    )
    assert s.is_tradable is True


def test_market_status_is_tradable_pre_market():
    s = ms.MarketStatus(
        market="extended-hours", early_hours=True, after_hours=False,
        server_time="", nyse="extended-hours", nasdaq="extended-hours",
    )
    assert s.is_tradable is True


def test_market_status_is_tradable_after_hours():
    s = ms.MarketStatus(
        market="extended-hours", early_hours=False, after_hours=True,
        server_time="", nyse="extended-hours", nasdaq="extended-hours",
    )
    assert s.is_tradable is True


def test_market_status_not_tradable_closed():
    s = ms.MarketStatus(
        market="closed", early_hours=False, after_hours=False,
        server_time="", nyse="closed", nasdaq="closed",
    )
    assert s.is_tradable is False


def test_today_holiday_name_matches_nyse():
    holidays = [
        ms.Holiday(date="2026-05-25", exchange="NYSE", name="Memorial Day", status="closed"),
        ms.Holiday(date="2026-05-25", exchange="NASDAQ", name="Memorial Day", status="closed"),
    ]
    assert ms.today_holiday_name(holidays, date(2026, 5, 25)) == "Memorial Day"


def test_today_holiday_name_returns_none_when_not_today():
    holidays = [
        ms.Holiday(date="2026-12-25", exchange="NYSE", name="Christmas", status="closed"),
    ]
    assert ms.today_holiday_name(holidays, date(2026, 5, 26)) is None


def test_today_holiday_name_skips_early_close():
    """Early-close days are not 'today is a holiday' — markets still partially open."""
    holidays = [
        ms.Holiday(
            date="2026-07-03", exchange="NYSE", name="Day before Independence Day",
            status="early-close", open="2026-07-03T09:30:00-04:00", close="2026-07-03T13:00:00-04:00",
        ),
    ]
    assert ms.today_holiday_name(holidays, date(2026, 7, 3)) is None


def test_today_holiday_name_skips_otc_only():
    """OTC-only closure should not flag today as closed for stocks."""
    holidays = [
        ms.Holiday(date="2026-05-25", exchange="OTC", name="Memorial Day", status="closed"),
    ]
    assert ms.today_holiday_name(holidays, date(2026, 5, 25)) is None


# ── next_open_date ────────────────────────────────────────────────────────────

def test_next_open_skips_weekend():
    """Friday → next open is Monday."""
    friday = date(2026, 5, 29)
    no_holidays: list[ms.Holiday] = []
    assert ms.next_open_date(no_holidays, friday) == date(2026, 6, 1)  # Monday


def test_next_open_skips_holiday():
    """Sunday 2026-05-24 + Memorial Day Monday 2026-05-25 → next open Tue 2026-05-26."""
    sunday = date(2026, 5, 24)
    holidays = [
        ms.Holiday(date="2026-05-25", exchange="NYSE", name="Memorial Day", status="closed"),
    ]
    assert ms.next_open_date(holidays, sunday) == date(2026, 5, 26)


def test_next_open_from_holiday_itself():
    """Memorial Day Monday → next open is Tuesday."""
    memorial_day = date(2026, 5, 25)
    holidays = [
        ms.Holiday(date="2026-05-25", exchange="NYSE", name="Memorial Day", status="closed"),
    ]
    assert ms.next_open_date(holidays, memorial_day) == date(2026, 5, 26)


def test_next_open_normal_weekday():
    """Tuesday → next open is Wednesday (no weekend or holiday between)."""
    tue = date(2026, 5, 26)
    assert ms.next_open_date([], tue) == date(2026, 5, 27)


# ── Live endpoint smoke tests ────────────────────────────────────────────────

def test_fetch_market_status_live():
    api_key = os.environ.get("POLYGON_API_KEY")
    if not api_key:
        pytest.skip("POLYGON_API_KEY not set")

    async def _go():
        async with aiohttp.ClientSession() as http:
            return await ms.fetch_market_status(http, api_key)

    status = asyncio.run(_go())
    print(f"\nLive market status: {status}")
    assert status is not None
    # market field is always populated
    assert status.market in ("open", "closed", "extended-hours"), (
        f"Unexpected market value: {status.market!r}"
    )
    assert isinstance(status.early_hours, bool)
    assert isinstance(status.after_hours, bool)
    assert status.server_time  # non-empty RFC3339


def test_fetch_upcoming_holidays_live():
    api_key = os.environ.get("POLYGON_API_KEY")
    if not api_key:
        pytest.skip("POLYGON_API_KEY not set")

    async def _go():
        async with aiohttp.ClientSession() as http:
            return await ms.fetch_upcoming_holidays(http, api_key)

    holidays = asyncio.run(_go())
    print(f"\nReturned {len(holidays)} upcoming holiday entries")
    for h in holidays[:5]:
        print(f"  {h.date}  {h.exchange:<6}  {h.status:<11}  {h.name}")
    # Holidays list may be empty if very end of year, but is normally populated
    assert isinstance(holidays, list)
    for h in holidays:
        assert h.date  # YYYY-MM-DD non-empty
        assert h.exchange
        assert h.status in ("closed", "early-close")
