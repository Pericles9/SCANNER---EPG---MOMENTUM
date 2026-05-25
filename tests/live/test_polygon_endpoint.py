"""Stage 1b: Raw endpoint verification.

Findings from Polygon (Massive) docs at /v2/snapshot/locale/us/markets/stocks/{direction}:
- URL path: /v2/snapshot/locale/us/markets/stocks/{direction}
- Top-level array key: "tickers"
- Field path: todaysChangePerc at top level of each ticker (not nested under day/lastTrade)
- Units: percentage values (sample doc shows 1849.096 = +1849%)
- Auth: apiKey query parameter (Polygon convention; not on this page but other endpoints)
- Result cap: 20 tickers per direction (hard cap)
- Off-hours: snapshot cleared at 03:30 AM ET, repopulates from ~04:00 AM ET

Env var: POLYGON_API_KEY (loaded via live system .env)

This is a live HTTP test. Run inside the trading container (which has aiohttp).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import aiohttp
import pytest

_GAINERS_URL = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/gainers"


async def _fetch_gainers(api_key: str) -> tuple[int, dict]:
    params = {"apiKey": api_key}
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession() as http:
        async with http.get(_GAINERS_URL, params=params, timeout=timeout) as resp:
            status = resp.status
            body = await resp.json()
            return status, body


def test_gainers_endpoint_returns_200_with_tickers_key():
    api_key = os.environ.get("POLYGON_API_KEY")
    if not api_key:
        pytest.skip("POLYGON_API_KEY not set")

    status, body = asyncio.run(_fetch_gainers(api_key))

    print(f"\nHTTP status: {status}")
    print(f"Response keys: {list(body.keys())}")
    print(f"Full response body:")
    print(json.dumps(body, indent=2)[:4000])  # cap output

    assert status == 200, f"Expected 200, got {status}: {body}"
    assert "tickers" in body, (
        f"Expected top-level 'tickers' key per docs, got keys: {list(body.keys())}"
    )

    tickers_raw = body["tickers"]
    print(f"\nReturned {len(tickers_raw)} tickers")

    for i, t in enumerate(tickers_raw[:5]):
        # Confirm field location
        top_level_pct = t.get("todaysChangePerc")
        nested_day_pct = t.get("day", {}).get("todaysChangePerc")
        print(
            f"  [{i}] {t.get('ticker'):>6}  "
            f"top-level todaysChangePerc={top_level_pct}  "
            f"day.todaysChangePerc={nested_day_pct}"
        )

    # Off-hours / weekend / holiday tolerance: if 0 returned, skip the size assertion
    if len(tickers_raw) == 0:
        print("\n[INFO] 0 tickers returned — likely off-hours / holiday. Skipping size assertion.")
        pytest.skip("No tickers returned (off-hours or holiday). Endpoint shape was verified.")

    # Otherwise: every ticker must have todaysChangePerc at the top level
    for t in tickers_raw:
        assert "todaysChangePerc" in t, (
            f"Ticker {t.get('ticker')} missing top-level todaysChangePerc: {t}"
        )
        assert isinstance(t["todaysChangePerc"], (int, float)), (
            f"todaysChangePerc must be numeric, got {type(t['todaysChangePerc'])}"
        )


if __name__ == "__main__":
    # Standalone mode for ad-hoc curl-style debugging
    api_key = os.environ.get("POLYGON_API_KEY")
    if not api_key:
        print("POLYGON_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    status, body = asyncio.run(_fetch_gainers(api_key))
    print(f"HTTP {status}")
    print(json.dumps(body, indent=2))
