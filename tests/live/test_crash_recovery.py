"""Tests for live.recovery.crash_recovery.

Everything is mocked — no real ib_insync connection, no real DB. ib_insync's
MarketOrder / LimitOrder / Stock are real (so we can assert order_type, tif,
outsideRth, lmtPrice), but the IB *client* is a fake whose placeOrder fills (or
doesn't) according to a per-test plan. The asyncpg pool/connection is faked
exactly like the other live tests.

Module timeouts are zeroed in a fixture so unfilled-order paths resolve instantly.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

import live.recovery.crash_recovery as cr
from live.recovery.crash_recovery import run_crash_recovery

_SD = date(2026, 6, 2)


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _OrderStatus:
    def __init__(self, status="Submitted", avg=0.0, filled=0):
        self.status = status
        self.avgFillPrice = avg
        self.filled = filled
        self.remaining = 0


class _OpenOrder:
    """Stand-in for an Order on an already-working trade (step 1)."""
    def __init__(self, order_id, action="SELL", qty=100, otype="LMT"):
        self.orderId = order_id
        self.permId = order_id
        self.action = action
        self.totalQuantity = qty
        self.orderType = otype


class _Contract:
    def __init__(self, symbol, sec_type="STK"):
        self.symbol = symbol
        self.secType = sec_type


class _Trade:
    def __init__(self, order, contract, status="Submitted"):
        self.order = order
        self.contract = contract
        self.orderStatus = _OrderStatus(status=status)


class _Position:
    def __init__(self, symbol, position, avg_cost=10.0, sec_type="STK"):
        self.contract = _Contract(symbol, sec_type)
        self.position = position
        self.avgCost = avg_cost


class _Ticker:
    def __init__(self, bid=0.0, ask=0.0, last=0.0):
        self.bid = bid
        self.ask = ask
        self.last = last


class FakeIB:
    def __init__(self, positions=None, open_trades=None, fill_plans=None,
                 ticker=None, qualify_fail=()):
        self._positions = positions or []
        self.open_trades = open_trades or []
        self._fill_plans = list(fill_plans or [])
        self._ticker = ticker or _Ticker()
        self._qualify_fail = set(qualify_fail)

        self.placed_orders = []      # ib_insync Order objects submitted
        self.placed_trades = []
        self.cancelled_order_ids = []
        self.global_cancel_calls = 0
        self.mkt_data_calls = 0

    async def reqAllOpenOrdersAsync(self):
        return self.open_trades

    def cancelOrder(self, order):
        self.cancelled_order_ids.append(getattr(order, "orderId", None))
        for tr in self.open_trades + self.placed_trades:
            if tr.order is order:
                tr.orderStatus.status = "Cancelled"

    def reqGlobalCancel(self):
        self.global_cancel_calls += 1

    async def reqPositionsAsync(self):
        return self._positions

    async def qualifyContractsAsync(self, contract):
        if contract.symbol in self._qualify_fail:
            raise RuntimeError(f"qualify failed for {contract.symbol}")
        return [contract]

    def placeOrder(self, contract, order):
        plan = self._fill_plans.pop(0) if self._fill_plans else {"fill": False}
        status = "Filled" if plan.get("fill") else "Submitted"
        tr = _Trade(order, contract, status=status)
        if plan.get("fill"):
            tr.orderStatus.avgFillPrice = plan.get("price", 0.0)
            tr.orderStatus.filled = order.totalQuantity
        self.placed_orders.append(order)
        self.placed_trades.append(tr)
        return tr

    def reqMktData(self, contract, *args):
        self.mkt_data_calls += 1
        return self._ticker

    def cancelMktData(self, contract):
        pass


class FakeConn:
    def __init__(self, owned=(), open_count=0, pending_count=0):
        self._owned = list(owned)
        self._open_count = open_count
        self._pending_count = pending_count
        self.executed = []   # list[(sql, args)]

    async def fetch(self, sql, *args):
        if "DISTINCT ticker" in sql:
            return [{"ticker": t} for t in self._owned]
        return []

    async def fetchval(self, sql, *args):
        if "FROM positions" in sql:
            return self._open_count
        if "FROM orders" in sql:
            return self._pending_count
        return 0

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    def transaction(self):
        class _T:
            async def __aenter__(self_):
                return None

            async def __aexit__(self_, *a):
                return False

        return _T()


class FakePool:
    def __init__(self, conn: FakeConn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self_):
                return conn

            async def __aexit__(self_, *a):
                return False

        return _Ctx()


# ── Fixtures / helpers ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _fast_timeouts(monkeypatch):
    for name in (
        "_CANCEL_CONFIRM_TIMEOUT_S", "_QUOTE_TIMEOUT_S",
        "_PRIMARY_TIMEOUT_S", "_SECONDARY_TIMEOUT_S", "_FINAL_TIMEOUT_S",
    ):
        monkeypatch.setattr(cr, name, 0.0)


def _set_session(monkeypatch, bucket, tradable=True):
    monkeypatch.setattr(cr, "current_session_bucket", lambda *a, **k: bucket)
    monkeypatch.setattr(cr, "is_tradable_now", lambda *a, **k: tradable)


def _exec_with(conn, needle):
    return [(s, a) for (s, a) in conn.executed if needle in s]


def _audit_rows(conn):
    return [a for (s, a) in conn.executed if "INSERT INTO signal_events" in s]


# ── 1. Clean start ──────────────────────────────────────────────────────────────

def test_clean_start_no_positions_no_writes(monkeypatch):
    _set_session(monkeypatch, "regular_hours", tradable=True)
    ib = FakeIB(positions=[], open_trades=[])
    conn = FakeConn(owned=[], open_count=0, pending_count=0)

    result = asyncio.run(run_crash_recovery(ib, FakePool(conn), None, _SD))

    assert result.had_open_positions is False
    assert ib.placed_orders == []
    assert conn.executed == []            # no DB writes on a clean start
    assert result.closed_tickers == []


# ── 2. Single RTH position (+ cancels a working order) ──────────────────────────

def test_single_rth_position_market_order(monkeypatch):
    _set_session(monkeypatch, "regular_hours", tradable=True)
    open_tr = _Trade(_OpenOrder(777), _Contract("AAPL"))
    ib = FakeIB(
        positions=[_Position("AAPL", 100, avg_cost=150.0)],
        open_trades=[open_tr],
        fill_plans=[{"fill": True, "price": 150.5}],
    )
    conn = FakeConn(owned=["AAPL"], open_count=1)

    result = asyncio.run(run_crash_recovery(ib, FakePool(conn), None, _SD))

    # working order cancelled and confirmed
    assert result.cancelled_orders == ["777"]
    assert ib.global_cancel_calls == 1
    # one market SELL submitted
    assert len(ib.placed_orders) == 1
    o = ib.placed_orders[0]
    assert o.orderType == "MKT"
    assert o.action == "SELL"
    assert o.totalQuantity == 100
    assert o.tif == "DAY"
    # closed + DB reconciled + audit row
    assert result.closed_tickers == ["AAPL"]
    assert _exec_with(conn, "UPDATE positions SET qty=0")
    audit = _audit_rows(conn)
    assert len(audit) == 1
    notes = json.loads(audit[0][9])
    assert notes["order_type"] == "MKT"
    assert notes["final_status"] == "CLOSED"


# ── 3. Single pre-market position → marketable EXT limit ────────────────────────

def test_single_pre_market_position_ext_limit(monkeypatch):
    _set_session(monkeypatch, "pre_market", tradable=True)
    ib = FakeIB(
        positions=[_Position("TSLA", 200, avg_cost=240.0)],
        fill_plans=[{"fill": True, "price": 239.9}],
        ticker=_Ticker(bid=240.0, ask=240.04, last=240.0),
    )
    conn = FakeConn(owned=["TSLA"], open_count=1)

    result = asyncio.run(run_crash_recovery(ib, FakePool(conn), None, _SD))

    assert result.closed_tickers == ["TSLA"]
    o = ib.placed_orders[0]
    assert o.orderType == "LMT"
    assert o.action == "SELL"
    assert o.tif == "EXT"
    assert o.outsideRth is True
    assert o.lmtPrice == pytest.approx(239.99)   # bid - 0.01


# ── 4. Illiquid pre-market — first limit fails, reprice fills ───────────────────

def test_illiquid_pre_market_reprices_once(monkeypatch):
    _set_session(monkeypatch, "pre_market", tradable=True)
    ib = FakeIB(
        positions=[_Position("ABC", 100, avg_cost=5.0)],
        fill_plans=[{"fill": False}, {"fill": True, "price": 5.05}],
        ticker=_Ticker(bid=5.10, ask=5.14, last=5.10),
    )
    conn = FakeConn(owned=["ABC"], open_count=1)

    result = asyncio.run(run_crash_recovery(ib, FakePool(conn), None, _SD))

    assert result.closed_tickers == ["ABC"]
    assert len(ib.placed_orders) == 2
    # primary at bid-0.01, reprice at bid-0.05
    assert ib.placed_orders[0].lmtPrice == pytest.approx(5.09)
    assert ib.placed_orders[1].lmtPrice == pytest.approx(5.05)
    notes = json.loads(_audit_rows(conn)[0][9])
    assert notes["reprice_count"] == 1


# ── 5. Stuck — never fills through the whole ladder ─────────────────────────────

def test_stuck_position_when_never_fills(monkeypatch):
    _set_session(monkeypatch, "pre_market", tradable=True)
    ib = FakeIB(
        positions=[_Position("ZZZ", 100, avg_cost=5.0)],
        fill_plans=[],            # every placeOrder → no fill
        ticker=_Ticker(bid=5.0, ask=5.04, last=5.0),
    )
    conn = FakeConn(owned=["ZZZ"], open_count=1)

    result = asyncio.run(run_crash_recovery(ib, FakePool(conn), None, _SD))

    assert result.stuck_tickers == ["ZZZ"]
    assert result.closed_tickers == []
    assert len(ib.placed_orders) == 3        # primary + two reprices
    notes = json.loads(_audit_rows(conn)[0][9])
    assert notes["final_status"] == "STUCK"


# ── 6. Outside trading hours → deferred, no order ───────────────────────────────

def test_outside_hours_defers(monkeypatch):
    _set_session(monkeypatch, "pre_market", tradable=False)
    ib = FakeIB(positions=[_Position("XYZ", 100)])
    conn = FakeConn(owned=["XYZ"], open_count=1)

    result = asyncio.run(run_crash_recovery(ib, FakePool(conn), None, _SD))

    assert result.deferred_tickers == ["XYZ"]
    assert result.stuck_tickers == []
    assert ib.placed_orders == []            # nothing submitted while market closed
    notes = json.loads(_audit_rows(conn)[0][9])
    assert notes["final_status"] == "DEFERRED"


# ── 7. IBKR orphan — closed, audit row tagged UNKNOWN ───────────────────────────

def test_ibkr_orphan_closed_with_unknown_strategy(monkeypatch):
    _set_session(monkeypatch, "regular_hours", tradable=True)
    ib = FakeIB(
        positions=[_Position("ORP", 50, avg_cost=3.0)],
        fill_plans=[{"fill": True, "price": 3.0}],
    )
    conn = FakeConn(owned=[], open_count=0)     # no DB row for ORP

    result = asyncio.run(run_crash_recovery(ib, FakePool(conn), None, _SD))

    assert result.closed_tickers == ["ORP"]
    audit = _audit_rows(conn)
    assert len(audit) == 1
    assert audit[0][0] == "UNKNOWN"             # strategy_id arg


# ── 8. DB shows open row, IBKR reports flat ─────────────────────────────────────

def test_db_open_but_ibkr_flat_cleans_db(monkeypatch):
    _set_session(monkeypatch, "regular_hours", tradable=True)
    ib = FakeIB(positions=[])                   # IBKR flat
    conn = FakeConn(owned=["STALE"], open_count=1)

    result = asyncio.run(run_crash_recovery(ib, FakePool(conn), None, _SD))

    assert result.had_open_positions is False
    assert ib.placed_orders == []               # no close order submitted
    assert _exec_with(conn, "UPDATE positions SET qty=0")   # DB still flattened
    assert _audit_rows(conn) == []              # nothing to audit (no IBKR position)


# ── 9. Multiple tickers, one errors mid-close ───────────────────────────────────

def test_multiple_tickers_one_errors(monkeypatch):
    _set_session(monkeypatch, "regular_hours", tradable=True)
    ib = FakeIB(
        positions=[
            _Position("A", 10, avg_cost=1.0),
            _Position("B", 20, avg_cost=2.0),
            _Position("C", 30, avg_cost=3.0),
        ],
        fill_plans=[{"fill": True, "price": 1.0}, {"fill": True, "price": 3.0}],
        qualify_fail={"B"},                     # B blows up during close
    )
    conn = FakeConn(owned=["A", "B", "C"], open_count=3)

    result = asyncio.run(run_crash_recovery(ib, FakePool(conn), None, _SD))

    assert result.error_tickers == ["B"]
    assert sorted(result.closed_tickers) == ["A", "C"]
    assert len(ib.placed_orders) == 2           # B never reached placeOrder


# ── 10. Pending orders in DB are cancelled ──────────────────────────────────────

def test_pending_db_orders_marked_cancelled(monkeypatch):
    _set_session(monkeypatch, "regular_hours", tradable=True)
    ib = FakeIB(positions=[])
    conn = FakeConn(owned=[], open_count=0, pending_count=2)

    asyncio.run(run_crash_recovery(ib, FakePool(conn), None, _SD))

    hits = _exec_with(conn, "UPDATE orders SET status='CANCELLED'")
    assert len(hits) == 1
    assert "CRASH_RECOVERY" in hits[0][0]
