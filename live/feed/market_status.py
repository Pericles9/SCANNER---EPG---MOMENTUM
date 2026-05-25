"""Massive (Polygon) market status + upcoming holidays.

Refreshed by scanner_monitor each poll. The /scanner Telegram command and other
status-aware code reads the module-level caches instead of guessing from a clock.

Endpoints:
  GET /v1/marketstatus/now       — current state (pre-market / regular / post / closed)
  GET /v1/marketstatus/upcoming  — array of forward-looking holiday closures
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp

log = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")

_STATUS_URL = "https://api.polygon.io/v1/marketstatus/now"
_HOLIDAYS_URL = "https://api.polygon.io/v1/marketstatus/upcoming"


@dataclass
class MarketStatus:
    market: str           # "open" | "closed" | "extended-hours"
    early_hours: bool
    after_hours: bool
    server_time: str      # RFC3339
    nyse: str
    nasdaq: str

    @property
    def is_tradable(self) -> bool:
        """True during pre-market, regular, or after-hours sessions (any open window)."""
        return (
            self.market in ("open", "extended-hours")
            or self.early_hours
            or self.after_hours
        )


@dataclass
class Holiday:
    date: str             # YYYY-MM-DD
    exchange: str         # NYSE | NASDAQ | OTC
    name: str
    status: str           # "closed" | "early-close"
    open: Optional[str] = None    # ISO8601 — present only for early-close
    close: Optional[str] = None


# ── Module-level cache (single-writer = scanner_monitor.refresh) ──────────────

last_market_status: Optional[MarketStatus] = None
upcoming_holidays: list[Holiday] = []
_holidays_last_fetched_date: Optional[date] = None


def get_last_market_status() -> Optional[MarketStatus]:
    return last_market_status


def get_upcoming_holidays() -> list[Holiday]:
    return upcoming_holidays


# ── Fetchers ──────────────────────────────────────────────────────────────────

async def fetch_market_status(
    http: aiohttp.ClientSession, api_key: str
) -> Optional[MarketStatus]:
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with http.get(_STATUS_URL, params={"apiKey": api_key}, timeout=timeout) as resp:
            if resp.status != 200:
                log.warning("market-status HTTP %s", resp.status)
                return None
            body = await resp.json()
    except Exception:
        log.exception("market-status fetch failed")
        return None

    exchanges = body.get("exchanges") or {}
    return MarketStatus(
        market=body.get("market", ""),
        early_hours=bool(body.get("earlyHours", False)),
        after_hours=bool(body.get("afterHours", False)),
        server_time=body.get("serverTime", ""),
        nyse=exchanges.get("nyse", ""),
        nasdaq=exchanges.get("nasdaq", ""),
    )


async def fetch_upcoming_holidays(
    http: aiohttp.ClientSession, api_key: str
) -> list[Holiday]:
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with http.get(_HOLIDAYS_URL, params={"apiKey": api_key}, timeout=timeout) as resp:
            if resp.status != 200:
                log.warning("upcoming-holidays HTTP %s", resp.status)
                return []
            body = await resp.json()
    except Exception:
        log.exception("upcoming-holidays fetch failed")
        return []

    items = body if isinstance(body, list) else []
    return [
        Holiday(
            date=item.get("date", ""),
            exchange=item.get("exchange", ""),
            name=item.get("name", ""),
            status=item.get("status", ""),
            open=item.get("open"),
            close=item.get("close"),
        )
        for item in items
    ]


async def refresh(http: aiohttp.ClientSession, api_key: str) -> None:
    """Refresh market status (every call) and holidays (once per ET date).

    Called by scanner_monitor._poll_once. Updates module-level caches in place.
    """
    global last_market_status, upcoming_holidays, _holidays_last_fetched_date

    status = await fetch_market_status(http, api_key)
    if status is not None:
        last_market_status = status

    today_et = datetime.now(_ET).date()
    if _holidays_last_fetched_date != today_et:
        holidays = await fetch_upcoming_holidays(http, api_key)
        if holidays:
            upcoming_holidays = holidays
            _holidays_last_fetched_date = today_et
            log.info("Upcoming holidays refreshed: %d entries", len(holidays))


# ── Pure helpers ──────────────────────────────────────────────────────────────

def today_holiday_name(
    holidays: list[Holiday], today_et: Optional[date] = None
) -> Optional[str]:
    """Return the holiday name if today is a full-closure day on NYSE/NASDAQ."""
    if today_et is None:
        today_et = datetime.now(_ET).date()
    today_str = today_et.isoformat()
    for h in holidays:
        if h.date == today_str and h.exchange in ("NYSE", "NASDAQ") and h.status == "closed":
            return h.name
    return None


def next_open_date(
    holidays: list[Holiday], today_et: Optional[date] = None
) -> Optional[date]:
    """Next weekday that is not a full NYSE/NASDAQ closure. Lookahead capped at 14 days."""
    if today_et is None:
        today_et = datetime.now(_ET).date()
    closures = {
        h.date for h in holidays
        if h.exchange in ("NYSE", "NASDAQ") and h.status == "closed"
    }
    candidate = today_et + timedelta(days=1)
    for _ in range(14):
        if candidate.weekday() < 5 and candidate.isoformat() not in closures:
            return candidate
        candidate += timedelta(days=1)
    return None
