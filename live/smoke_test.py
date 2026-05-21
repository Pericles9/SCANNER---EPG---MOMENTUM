"""Pre-flight smoke test for EPG live paper trading system.

Run from repo root:
    python live/smoke_test.py

Exit 0 if all checks pass. Exit 1 if any fail.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import nest_asyncio
nest_asyncio.apply()

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

_LIVE_DIR = _REPO_ROOT / "live"
_GREEN = "\033[32m"
_RED = "\033[31m"
_RESET = "\033[0m"
_PASS = f"{_GREEN}PASS{_RESET}"
_FAIL = f"{_RED}FAIL{_RESET}"


def _load_dotenv() -> None:
    env_file = _LIVE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_results: list[bool] = []


def _print_result(n: int, label: str, ok: bool, detail: str = "", fix: str = "") -> None:
    total = 6
    marker = _PASS if ok else _FAIL
    padded = f"{label} ".ljust(46, ".")
    line = f"[{n}/{total}] {padded} {marker}"
    if detail:
        line += f"  ({detail})"
    print(line)
    if not ok and (detail or fix):
        if detail:
            print(f"      Error: {detail}")
        if fix:
            for i, fline in enumerate(fix.splitlines()):
                prefix = "      Fix:   " if i == 0 else "             "
                print(f"{prefix}{fline}")
    _results.append(ok)


# ── Check 1 — Config ──────────────────────────────────────────────────────────

def _check_config() -> None:
    try:
        from live.config import CFG  # noqa: F401
        _print_result(1, "Config loads cleanly", True)
    except Exception as exc:
        _print_result(1, "Config loads cleanly", False,
                      str(exc),
                      "Fix strategy.json — check all required fields are present and valid")


# ── Check 2 — No REQUIRED_FROM_BACKTEST ──────────────────────────────────────

def _check_required_from_backtest() -> None:
    strategy_file = _LIVE_DIR / "strategy.json"
    try:
        text = strategy_file.read_text()
    except FileNotFoundError:
        _print_result(2, "No REQUIRED_FROM_BACKTEST in strategy.json", False,
                      "strategy.json not found", "Ensure live/strategy.json exists")
        return
    sentinel = "REQUIRED_FROM_BACKTEST"
    if sentinel in text:
        hits = [ln.strip() for ln in text.splitlines() if sentinel in ln]
        _print_result(2, "No REQUIRED_FROM_BACKTEST in strategy.json", False,
                      f"{len(hits)} field(s) still unset: {hits[0][:60]}",
                      "Fill in all REQUIRED_FROM_BACKTEST values in strategy.json")
    else:
        _print_result(2, "No REQUIRED_FROM_BACKTEST in strategy.json", True)


# ── Check 3 — PostgreSQL ─────────────────────────────────────────────────────

_ALL_TABLES = [
    "strategies", "scanner_snapshots", "ticks", "quotes",
    "positions", "orders", "trades", "sessions",
    "hawkes_refits", "signal_events",
]

_MIGRATED_COLUMNS: dict[str, list[str]] = {
    "orders":   ["filled_qty", "remaining_qty", "expected_price", "slippage_bps"],
    "sessions": ["account_equity_start", "theoretical_equity_start", "theoretical_equity_end"],
}


async def _check_postgres() -> None:
    import asyncpg

    db_url = (
        os.environ.get("DB_URL_LOCAL")
        or os.environ.get("DB_URL", "").replace("@db:5432", "@localhost:5433")
    )
    if not db_url:
        _print_result(3, "PostgreSQL connects + all tables present", False,
                      "DB_URL not set",
                      "Set DB_URL or DB_URL_LOCAL in live/.env or environment")
        return

    try:
        conn = await asyncpg.connect(db_url, timeout=10)
    except Exception as exc:
        _print_result(3, "PostgreSQL connects + all tables present", False,
                      str(exc),
                      "Ensure PostgreSQL is running and DB_URL is correct\n"
                      "Docker: docker-compose -f live/docker-compose.yml up -d db")
        return

    try:
        missing_tables: list[str] = []
        for table in _ALL_TABLES:
            try:
                await conn.execute(f"SELECT 1 FROM {table} LIMIT 0")
            except Exception:
                missing_tables.append(table)

        if missing_tables:
            _print_result(3, "PostgreSQL connects + all tables present", False,
                          f"missing tables: {', '.join(missing_tables)}",
                          "psql $DB_URL -f live/init_db.sql")
            return

        missing_cols: list[str] = []
        for table, cols in _MIGRATED_COLUMNS.items():
            rows = await conn.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name = $1",
                table,
            )
            existing = {r["column_name"] for r in rows}
            for col in cols:
                if col not in existing:
                    missing_cols.append(f"{table}.{col}")

        if missing_cols:
            _print_result(3, "PostgreSQL connects + all tables present", False,
                          f"column \"{missing_cols[0]}\" not found in {missing_cols[0].split('.')[0]}",
                          "Run live/db/migrate_v1.sql against your database\n"
                          "psql $DB_URL -f live/db/migrate_v1.sql")
        else:
            _print_result(3, "PostgreSQL connects + all tables present", True)
    finally:
        await conn.close()


# ── Checks 4 + 5 — IBKR ──────────────────────────────────────────────────────

async def _check_ibkr() -> None:
    host = os.environ.get("IBKR_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("IBKR_PORT", "4002"))
        client_id = int(os.environ.get("IBKR_CLIENT_ID", "1"))
    except ValueError:
        _print_result(4, "IB Gateway connects", False,
                      "IBKR_PORT or IBKR_CLIENT_ID is not a valid integer")
        _print_result(5, "IBKR account equity query returns value", False, "skipped")
        return

    try:
        from ib_insync import IB
        ib = IB()
        await ib.connectAsync(host, port, clientId=client_id, timeout=10, readonly=True)
    except Exception as exc:
        _print_result(4, "IB Gateway connects", False,
                      str(exc),
                      f"IB Gateway not running or API not enabled on {host}:{port}\n"
                      "Start IB Gateway and confirm API port is set to 4002 (paper) or 4001 (live)")
        _print_result(5, "IBKR account equity query returns value", False,
                      "skipped — IB Gateway not connected")
        return

    _print_result(4, "IB Gateway connects", True)

    try:
        vals = ib.accountValues()
        equity = next(
            (float(v.value) for v in vals
             if v.tag == "NetLiquidation" and v.currency == "USD"),
            0.0,
        )
        if equity > 0:
            _print_result(5, "IBKR account equity query returns value", True,
                          f"${equity:,.2f}")
        else:
            _print_result(5, "IBKR account equity query returns value", False,
                          "NetLiquidation returned 0 or not found",
                          "Check IB Gateway paper account is logged in and account data is loaded")
    except Exception as exc:
        _print_result(5, "IBKR account equity query returns value", False, str(exc))
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


# ── Check 6 — Polygon WebSocket ───────────────────────────────────────────────

async def _check_polygon_ws() -> None:
    import aiohttp

    api_key = os.environ.get("POLYGON_API_KEY", "")
    if not api_key:
        _print_result(6, "Polygon WebSocket receives first tick", False,
                      "POLYGON_API_KEY not set",
                      "Set POLYGON_API_KEY in live/.env or environment")
        return

    ws_url = "wss://socket.polygon.io/stocks"
    tick_timeout_s = 10.0

    try:
        timeout = aiohttp.ClientTimeout(total=tick_timeout_s + 5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(ws_url) as ws:
                await ws.send_str(json.dumps({"action": "auth", "params": api_key}))
                await ws.send_str(json.dumps({"action": "subscribe", "params": "T.SPY"}))

                tick: dict | None = None
                deadline = asyncio.get_event_loop().time() + tick_timeout_s

                while asyncio.get_event_loop().time() < deadline:
                    remaining = deadline - asyncio.get_event_loop().time()
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=min(2.0, remaining))
                    except asyncio.TimeoutError:
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        for item in json.loads(msg.data):
                            ev = item.get("ev")
                            if ev == "T":
                                tick = item
                                break
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                    if tick:
                        break

                try:
                    await ws.send_str(json.dumps({"action": "unsubscribe", "params": "T.SPY"}))
                except Exception:
                    pass

        if tick:
            sym = tick.get("sym", "?")
            price = tick.get("p", 0.0)
            _print_result(6, "Polygon WebSocket receives first tick", True,
                          f"{sym} T {price:.2f}")
        else:
            _print_result(6, "Polygon WebSocket receives first tick", False,
                          f"no T.SPY tick received in {tick_timeout_s:.0f}s "
                          "(expected if run before 4:00am ET pre-market open)",
                          "Verify POLYGON_API_KEY has WebSocket access (Starter tier+)\n"
                          "Re-run after 4:00am ET when pre-market data begins")
    except Exception as exc:
        _print_result(6, "Polygon WebSocket receives first tick", False,
                      str(exc),
                      "Check POLYGON_API_KEY and network connectivity")


# ── Main ──────────────────────────────────────────────────────────────────────

async def _main() -> bool:
    _load_dotenv()

    _check_config()
    _check_required_from_backtest()
    await _check_postgres()
    await _check_ibkr()
    await _check_polygon_ws()

    print("-" * 53)
    if all(_results):
        print("All checks passed. System ready.")
        return True
    else:
        n_fail = sum(1 for r in _results if not r)
        print(f"{n_fail} check(s) failed. Fix above issues before starting trading.")
        return False


if __name__ == "__main__":
    ok = asyncio.run(_main())
    sys.exit(0 if ok else 1)
