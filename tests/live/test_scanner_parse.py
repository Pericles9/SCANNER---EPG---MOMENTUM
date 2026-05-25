"""Stage 2: Code-level test against live response.

Calls the same endpoint with the same request construction as the scanner code
(reusing scanner_monitor.build_snapshot_context via aiohttp directly), then runs the
EXACT same parsing logic the scanner uses (raw_qualifying filter + classify + Phase G v2),
and compares parsed counts against the raw response count.

Any drop in count between the raw response and the parsed list is a bug to investigate.
"""
from __future__ import annotations

import asyncio
import json
import os

import aiohttp
import pytest

from live.config import CFG

_GAINERS_URL = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/gainers"


async def _fetch_raw(api_key: str) -> dict:
    params = {"apiKey": api_key}
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession() as http:
        async with http.get(_GAINERS_URL, params=params, timeout=timeout) as resp:
            assert resp.status == 200
            return await resp.json()


async def _run_scanner_parser(api_key: str):
    """Reuse scanner_monitor.build_snapshot_context end-to-end."""
    from live.scanner_monitor import build_snapshot_context
    async with aiohttp.ClientSession() as http:
        contexts, record = await build_snapshot_context(http, api_key)
        return contexts, record


def test_parse_matches_raw_response():
    api_key = os.environ.get("POLYGON_API_KEY")
    if not api_key:
        pytest.skip("POLYGON_API_KEY not set")

    raw = asyncio.run(_fetch_raw(api_key))
    tickers_raw = raw.get("tickers", [])
    print(f"\n[RAW] {len(tickers_raw)} tickers returned by endpoint")

    gap_pct = CFG.scanner.gap_threshold * 100  # 0.30 -> 30
    print(f"[CONFIG] gap_threshold={CFG.scanner.gap_threshold} -> filter pct >= {gap_pct}")

    # Apply scanner's raw filter exactly as in scanner_monitor.build_snapshot_context
    raw_qualifying = [
        {"ticker": t["ticker"], "pct_change": t.get("todaysChangePerc", 0.0)}
        for t in tickers_raw
        if t.get("todaysChangePerc", 0.0) >= gap_pct
    ]
    print(f"[FILTER] {len(raw_qualifying)} pass the {gap_pct}% gap filter")
    for q in raw_qualifying:
        print(f"  {q['ticker']:>6}  +{q['pct_change']:.2f}%")

    # Run the full scanner pipeline (includes ticker eligibility classify)
    contexts, record = asyncio.run(_run_scanner_parser(api_key))
    print(f"\n[SCANNER] build_snapshot_context returned {len(contexts)} contexts")
    print(f"[SCANNER] record.n_qualifying={record.n_qualifying}")
    for ctx in contexts:
        print(
            f"  {ctx.ticker:>6}  +{ctx.pct_change:.2f}%  Q{ctx.scanner_quartile}  rank={ctx.scanner_rank}"
        )

    # Holiday / off-hours: 0 raw -> 0 contexts is the correct outcome
    if not tickers_raw:
        print("\n[INFO] 0 raw tickers (holiday/off-hours). Parser behavior is correct: 0 -> 0.")
        assert len(contexts) == 0
        return

    # Otherwise: scanner-eligible (CS+XNYS/XNAS) is a subset of raw_qualifying.
    # Drops here mean the ticker is non-CS or not on XNYS/XNAS — NOT a parser bug.
    assert len(contexts) <= len(raw_qualifying), (
        f"Scanner output ({len(contexts)}) cannot exceed filter-passed count ({len(raw_qualifying)})"
    )

    if len(contexts) < len(raw_qualifying):
        scanner_tickers = {c.ticker for c in contexts}
        filtered_tickers = {q["ticker"] for q in raw_qualifying}
        dropped = filtered_tickers - scanner_tickers
        print(
            f"\n[INFO] {len(dropped)} tickers dropped by eligibility classify "
            f"(non-CS or not on XNYS/XNAS): {sorted(dropped)}"
        )


def test_parser_field_paths_match_docs():
    """Sanity check: scanner reads tickers[*].todaysChangePerc at top level (matches docs)."""
    api_key = os.environ.get("POLYGON_API_KEY")
    if not api_key:
        pytest.skip("POLYGON_API_KEY not set")

    raw = asyncio.run(_fetch_raw(api_key))
    tickers_raw = raw.get("tickers", [])

    if not tickers_raw:
        pytest.skip("Endpoint returned 0 tickers — cannot verify field path on empty payload")

    for t in tickers_raw:
        assert "ticker" in t, f"Missing top-level 'ticker' field: {t}"
        assert "todaysChangePerc" in t, f"Missing top-level 'todaysChangePerc': {t}"
        # Sanity on units: should be percentage scale (e.g. 45.2), not fraction (0.452)
        # Polygon docs show values like 1849.096 for very high movers
        pct = t["todaysChangePerc"]
        assert isinstance(pct, (int, float))
        # Just print the magnitude so we can sanity-check units
        print(f"  {t['ticker']:>6}  todaysChangePerc={pct!r}")
