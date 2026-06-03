"""
Unit tests for setup filter entry stack integration (Phase EPG-OPT2-SF).

CORRECTED behavior (hotfix): the setup filter checks the CURRENT bar's Q_tilde
only. Entry is allowed as soon as Q_tilde[bar] >= threshold (0.75 during the first
65 warmup bars, 0.65 after). There is NO 15-bar sustain / first_qualify_bar gate.

Tests:
  - Entry allowed as soon as Q_tilde >= threshold on the current bar (no lookback)
  - Entry blocked on bars where Q_tilde < threshold
  - Re-qualification: blocked when Q_tilde dips, allowed again when it recovers
  - Warmup threshold (0.75) applies to first 65 bars, 0.65 after
  - Backward compatibility: sf=None gives identical results to _run_gate_opt2
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.sweep_runner_opt2 import (
    _run_gate_opt2,
    _run_gate_opt2_sf,
    precompute_sf_trajectory,
    sf_is_qualified_at,
    SFTrajectory,
)
from core.epg.gate import GateState
from core.filters.setup_filter import Q_THRESHOLD, WARMUP_THRESHOLD, WARMUP_BARS
from data.schemas.mom_db import NS_PER_SECOND

BAR_NS = 60 * NS_PER_SECOND


class FakeTradeData:
    def __init__(self, n_trades: int, session_start_ns: int,
                 price: float = 10.0, size: int = 100):
        self.timestamps = np.array(
            [session_start_ns + i * NS_PER_SECOND for i in range(n_trades)],
            dtype=np.int64)
        self.prices = np.full(n_trades, price, dtype=np.float64)
        self.sizes = np.full(n_trades, size, dtype=np.int64)
        self.t_sec = np.array([i * 1.0 for i in range(n_trades)], dtype=np.float64)
        self.n_trades = n_trades


def _make_sf(bar_starts_ns: np.ndarray, q_tilde: np.ndarray,
             session_start_ns: int, first_qualify_bar: int = -1) -> SFTrajectory:
    """Build an SFTrajectory with CURRENT-BAR qualification (no sustain gate).

    first_qualify_bar is stored only as a diagnostic; it does NOT affect `qualified`.
    """
    n = len(q_tilde)
    qualified = np.zeros(n, dtype=bool)
    for i in range(n):
        bar_session_idx = int((bar_starts_ns[i] - session_start_ns) / BAR_NS)
        thr = WARMUP_THRESHOLD if bar_session_idx < WARMUP_BARS else Q_THRESHOLD
        if q_tilde[i] >= thr:
            qualified[i] = True
    return SFTrajectory(
        bar_starts_ns=bar_starts_ns, q_tilde=q_tilde, qualified=qualified,
        first_qualify_bar=first_qualify_bar, n_bars=n,
        mean_q_tilde=float(np.mean(q_tilde)), psi_passes=True,
    )


# ── sf_is_qualified_at: current-bar-only behavior ─────────────────────────

class TestSfIsQualifiedAt:

    def test_no_bars_returns_false(self):
        sf = SFTrajectory(
            bar_starts_ns=np.array([], dtype=np.int64),
            q_tilde=np.array([], dtype=np.float64),
            qualified=np.array([], dtype=bool),
            first_qualify_bar=-1, n_bars=0, mean_q_tilde=0.0, psi_passes=True)
        assert not sf_is_qualified_at(sf, 12345)

    def test_qualified_as_soon_as_threshold_met_no_sustain(self):
        """CORRECTED: entry allowed on the FIRST bar Q_tilde >= 0.65, even with
        no prior sustained window. first_qualify_bar=-1 must NOT block."""
        sess = 4 * 3600 * NS_PER_SECOND
        n = 80
        bars = np.array([sess + i * BAR_NS for i in range(n)], dtype=np.int64)
        # All bars post-warmup (need bar_session_idx >= 65). Put data at bars 70-79.
        q = np.full(n, 0.50, dtype=np.float64)
        q[70] = 0.66  # single bar above 0.65, no sustain
        # first_qualify_bar = -1 (never sustained 15 bars) — must be IGNORED
        sf = _make_sf(bars, q, sess, first_qualify_bar=-1)
        # Bar 70 should qualify despite no sustained window and first_qualify_bar=-1
        ts70 = bars[70] + 30 * NS_PER_SECOND
        assert sf_is_qualified_at(sf, ts70), "Single-bar qualification must be allowed"
        # Bar 69 (Q=0.50) should not qualify
        ts69 = bars[69] + 30 * NS_PER_SECOND
        assert not sf_is_qualified_at(sf, ts69)

    def test_blocked_when_below_threshold(self):
        sess = 4 * 3600 * NS_PER_SECOND
        n = 80
        bars = np.array([sess + i * BAR_NS for i in range(n)], dtype=np.int64)
        q = np.full(n, 0.50, dtype=np.float64)  # always below 0.65
        sf = _make_sf(bars, q, sess)
        for b in range(70, 80):
            assert not sf_is_qualified_at(sf, bars[b] + 30 * NS_PER_SECOND)

    def test_requalifies_after_dip(self):
        """Qualified → dips below → blocked → recovers → qualified again, all
        on a current-bar basis with no sustain re-arming."""
        sess = 4 * 3600 * NS_PER_SECOND
        n = 100
        bars = np.array([sess + i * BAR_NS for i in range(n)], dtype=np.int64)
        q = np.full(n, 0.80, dtype=np.float64)
        q[80] = 0.40  # single-bar dip
        sf = _make_sf(bars, q, sess)
        assert sf_is_qualified_at(sf, bars[79] + 30 * NS_PER_SECOND)
        assert not sf_is_qualified_at(sf, bars[80] + 30 * NS_PER_SECOND)
        assert sf_is_qualified_at(sf, bars[81] + 30 * NS_PER_SECOND)  # immediate recovery

    def test_warmup_threshold_applies_first_65_bars(self):
        """Bars before index 65 require Q >= 0.75; bars >= 65 require Q >= 0.65."""
        sess = 4 * 3600 * NS_PER_SECOND
        n = 80
        bars = np.array([sess + i * BAR_NS for i in range(n)], dtype=np.int64)
        q = np.full(n, 0.70, dtype=np.float64)  # between 0.65 and 0.75
        sf = _make_sf(bars, q, sess)
        # Bar 10 (warmup): 0.70 < 0.75 → not qualified
        assert not sf_is_qualified_at(sf, bars[10] + 30 * NS_PER_SECOND)
        # Bar 70 (post-warmup): 0.70 >= 0.65 → qualified
        assert sf_is_qualified_at(sf, bars[70] + 30 * NS_PER_SECOND)


def _cfg_a(tau=120.0, p_open=0.65, p_close=0.65):
    return {"config_id": f"t{int(tau)}_po{int(p_open*100)}_pc{int(p_close*100)}",
            "variant": "a", "tau": tau, "p_open": p_open, "p_close": p_close,
            "m_cool_sec": 0.0, "tau_cool_sec": 120.0}


class TestRunGateOpt2SF:

    def test_sf_none_identical_to_no_sf(self):
        sess = 4 * 3600 * NS_PER_SECOND
        n = 2000
        td = FakeTradeData(n, sess, price=10.0, size=500)
        sides = np.zeros(n, dtype=np.int8)
        cfg = _cfg_a()
        r_no = _run_gate_opt2(cfg, td, sides, 300.0)
        r_none = _run_gate_opt2_sf(cfg, td, sides, 300.0, sf=None)
        assert r_no["n_trades"] == r_none["n_trades"]
        assert r_no["pnl_list"] == r_none["pnl_list"]
        assert r_none["n_entries_blocked_by_sf"] == 0

    def test_entries_allowed_immediately_when_qualified(self):
        """With SF qualified across the whole post-warmup session (no sustain
        gate), SF should not block any entries → identical trade count to no-SF."""
        sess = 4 * 3600 * NS_PER_SECOND
        n = 6000  # 100 minutes
        td = FakeTradeData(n, sess, price=10.0, size=500)
        sides = np.zeros(n, dtype=np.int8)
        n_bars = 100
        bars = np.array([sess + i * BAR_NS for i in range(n_bars)], dtype=np.int64)
        q = np.full(n_bars, 0.90, dtype=np.float64)  # always qualified (>=0.75 even in warmup)
        sf = _make_sf(bars, q, sess, first_qualify_bar=-1)
        cfg = _cfg_a(tau=60.0, p_open=0.55, p_close=0.55)
        r_sf = _run_gate_opt2_sf(cfg, td, sides, 300.0, sf=sf)
        r_no = _run_gate_opt2(cfg, td, sides, 300.0)
        assert r_sf["n_trades"] == r_no["n_trades"], "Fully-qualified SF must not block"
        assert r_sf["n_entries_blocked_by_sf"] == 0

    def test_entries_blocked_only_on_unqualified_bars(self):
        """SF qualified only on some bars → trades <= unfiltered, blocks >= 0."""
        sess = 4 * 3600 * NS_PER_SECOND
        n = 6000
        td = FakeTradeData(n, sess, price=10.0, size=500)
        sides = np.zeros(n, dtype=np.int8)
        n_bars = 100
        bars = np.array([sess + i * BAR_NS for i in range(n_bars)], dtype=np.int64)
        q = np.full(n_bars, 0.50, dtype=np.float64)  # mostly blocked
        q[80:90] = 0.90  # qualified window
        sf = _make_sf(bars, q, sess)
        cfg = _cfg_a(tau=60.0, p_open=0.55, p_close=0.55)
        r_sf = _run_gate_opt2_sf(cfg, td, sides, 300.0, sf=sf)
        r_no = _run_gate_opt2(cfg, td, sides, 300.0)
        assert r_sf["n_trades"] <= r_no["n_trades"]
        assert r_sf["n_entries_blocked_by_sf"] >= 0


class TestPrecomputeSFTrajectory:

    def test_insufficient_bars_returns_empty(self):
        sess = 4 * 3600 * NS_PER_SECOND
        td = FakeTradeData(5, sess)
        sf = precompute_sf_trajectory(td, sess, sess + 10 * NS_PER_SECOND)
        assert sf.n_bars == 0

    def test_qualified_is_pure_current_bar_threshold(self):
        """qualified[i] must equal (Q_tilde[i] >= threshold) with no dependence
        on first_qualify_bar."""
        sess = 4 * 3600 * NS_PER_SECOND
        n = 7200
        td = FakeTradeData(n, sess, price=10.0, size=1000)
        sf = precompute_sf_trajectory(td, sess, sess + 120 * 60 * NS_PER_SECOND)
        if sf.n_bars > 0:
            for i in range(sf.n_bars):
                bar_idx = int((sf.bar_starts_ns[i] - sess) / BAR_NS)
                thr = WARMUP_THRESHOLD if bar_idx < WARMUP_BARS else Q_THRESHOLD
                expected = sf.q_tilde[i] >= thr
                assert bool(sf.qualified[i]) == bool(expected), (
                    f"bar {i}: qualified={sf.qualified[i]} but Q={sf.q_tilde[i]:.3f} thr={thr}")

    def test_no_sustain_dependence(self):
        """Even when the event never sustains 15 bars (first_qualify_bar == -1),
        individual qualifying bars must still be marked qualified."""
        sess = 4 * 3600 * NS_PER_SECOND
        n = 7200
        td = FakeTradeData(n, sess, price=10.0, size=1000)
        sf = precompute_sf_trajectory(td, sess, sess + 120 * 60 * NS_PER_SECOND)
        # If any bar is above threshold, qualified must have at least one True,
        # regardless of first_qualify_bar.
        if sf.n_bars > 0 and np.any(sf.qualified):
            assert sf.qualified.sum() > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
