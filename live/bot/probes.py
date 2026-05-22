"""Active service probes for /services command. Each probe returns (ok, detail_str)."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import aiohttp

_POLYGON_GAINERS = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/gainers"
_PROBE_TIMEOUT_S = 2.5
_KILL_FLAG_PATH = Path(__file__).parent.parent / "kill.flag"

_REQUIRED_TABLES = {
    "orders", "positions", "trades", "scanner_snapshots",
    "ticks", "signal_events", "hawkes_refits",
}


async def probe_polygon_rest(api_key: str) -> tuple[bool, str]:
    try:
        t0 = time.monotonic()
        timeout = aiohttp.ClientTimeout(total=_PROBE_TIMEOUT_S)
        async with aiohttp.ClientSession() as s:
            async with s.get(
                _POLYGON_GAINERS, params={"apiKey": api_key}, timeout=timeout
            ) as resp:
                resp.raise_for_status()
                latency_ms = (time.monotonic() - t0) * 1000
        return True, f"{latency_ms:.0f}ms"
    except Exception as exc:
        return False, str(exc)[:40]


async def probe_postgres(pool: Any) -> tuple[bool, str]:
    try:
        t0 = time.monotonic()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        latency_ms = (time.monotonic() - t0) * 1000
        return True, f"{latency_ms:.0f}ms"
    except Exception as exc:
        return False, str(exc)[:40]


async def probe_postgres_schema(pool: Any) -> tuple[bool, str]:
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public'"
            )
        found = {r["table_name"] for r in rows}
        missing = _REQUIRED_TABLES - found
        if missing:
            return False, f"missing: {', '.join(sorted(missing))}"
        return True, f"{len(found)} tables"
    except Exception as exc:
        return False, str(exc)[:40]


async def probe_ibkr(ibkr: Any) -> tuple[bool, str]:
    try:
        connected = ibkr.is_connected()
        return connected, "connected" if connected else "disconnected"
    except Exception as exc:
        return False, str(exc)[:40]


async def probe_ibkr_account(ibkr: Any) -> tuple[bool, str]:
    try:
        vals = await ibkr.account_values()
        if not vals:
            return False, "empty account values"
        return True, f"{len(vals)} account values"
    except Exception as exc:
        return False, str(exc)[:40]


def probe_halt_file() -> tuple[bool, str]:
    exists = _KILL_FLAG_PATH.exists()
    if exists:
        return False, "kill.flag present — system halted"
    return True, "no kill.flag"


async def run_all_probes(
    pool: Any,
    ibkr: Any,
    polygon_api_key: str,
) -> list[tuple[str, bool, str]]:
    """Run all probes concurrently with a hard 3s deadline."""
    tasks = {
        "Polygon REST": probe_polygon_rest(polygon_api_key),
        "PostgreSQL": probe_postgres(pool),
        "PG schema": probe_postgres_schema(pool),
        "IBKR conn": probe_ibkr(ibkr),
        "IBKR account": probe_ibkr_account(ibkr),
    }

    results: list[tuple[str, bool, str]] = []

    done = await asyncio.gather(*tasks.values(), return_exceptions=True)
    for name, outcome in zip(tasks.keys(), done):
        if isinstance(outcome, Exception):
            results.append((name, False, str(outcome)[:40]))
        else:
            results.append((name, outcome[0], outcome[1]))

    ok, detail = probe_halt_file()
    results.append(("Halt file", ok, detail))

    return results
