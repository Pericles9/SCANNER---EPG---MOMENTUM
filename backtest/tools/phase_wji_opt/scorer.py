"""
Metric computation and Borda selection for Phase WJI-OPT (T2b / T2c).

Public API
----------
compute_metrics(trades, thresholds)  -> dict
    Compute full metric panel for one config given a list of trade dicts.
    Each trade dict must have keys: pnl_pct, available_move_pct, year.

compute_per_year(trades)  -> dict[str, dict]
    Per-year metric panels (same structure as compute_metrics result).

apply_hard_filters(configs, thresholds)  -> list[str]
    Return config_ids that pass all hard filters.

borda_rank(configs, survivors, metrics_by_config)  -> list[str]
    Rank surviving configs by Borda score over {capture_fraction, ev, cvar5_pct}.
    Ties broken by median_pct then n_trades.

select_winner(metrics_by_config, thresholds)  -> str | None
    Full pipeline: hard filters → Borda → return winner config_id.
"""
from __future__ import annotations

import math
import statistics
from typing import Optional


# ══════════════════════════════════════════════════════════════════════
#  Core metric computation
# ══════════════════════════════════════════════════════════════════════

def _cvar5(pnl_list: list[float]) -> tuple[float, int]:
    """
    Return (cvar5_pct, n_tail_trades).

    cvar5_pct = mean of the worst floor(0.05 * n) trades (min 1).
    """
    n = len(pnl_list)
    if n == 0:
        return (float("nan"), 0)
    k = max(1, math.floor(0.05 * n))
    tail = sorted(pnl_list)[:k]
    return (sum(tail) / len(tail), len(tail))


def _profit_factor(pnl_list: list[float]) -> float:
    wins = sum(p for p in pnl_list if p > 0)
    losses = sum(abs(p) for p in pnl_list if p < 0)
    if losses == 0:
        return float("inf") if wins > 0 else float("nan")
    return wins / losses


def compute_metrics(
    trades: list[dict],
    thresholds: Optional[dict] = None,
) -> dict:
    """
    Compute the full metric panel for one config.

    Parameters
    ----------
    trades : list of dicts, each with keys:
        pnl_pct             float  — per-trade PnL as a percentage
        available_move_pct  float  — hindsight available move from entry, floored at 0
        year                str    — calendar year of the event
        ticker              str    — optional, used for worst_event
        date                str    — optional, used for worst_event
    thresholds : optional dict (used externally for filter step, not here)

    Returns
    -------
    dict with keys: n_trades, capture_fraction, ev, cvar5_pct, max_loss_pct,
                    worst_event, median_pct, pf, n_cvar5_trades
    worst_event: {ticker, date, pnl_pct} of the trade with lowest pnl_pct.
    """
    if not trades:
        return {
            "n_trades": 0,
            "capture_fraction": None,
            "ev": None,
            "cvar5_pct": None,
            "max_loss_pct": None,
            "worst_event": None,
            "median_pct": None,
            "pf": None,
            "n_cvar5_trades": 0,
        }

    pnl = [t["pnl_pct"] for t in trades]
    avail = [t["available_move_pct"] for t in trades]

    n = len(pnl)
    sum_avail = sum(avail)

    # capture_fraction: ratio-of-means
    if sum_avail > 0:
        capture_fraction = sum(pnl) / sum_avail
    else:
        capture_fraction = None

    cvar5, n_cvar5 = _cvar5(pnl)

    worst_idx = min(range(n), key=lambda i: pnl[i])
    worst_trade = trades[worst_idx]
    worst_event = {
        "ticker": worst_trade.get("ticker"),
        "date": worst_trade.get("date"),
        "pnl_pct": pnl[worst_idx],
    }

    return {
        "n_trades": n,
        "capture_fraction": capture_fraction,
        "ev": sum(pnl) / n,
        "cvar5_pct": cvar5,
        "max_loss_pct": min(pnl),
        "worst_event": worst_event,
        "median_pct": statistics.median(pnl),
        "pf": _profit_factor(pnl),
        "n_cvar5_trades": n_cvar5,
    }


def compute_per_year(trades: list[dict]) -> dict[str, dict]:
    """
    Compute metric panel per calendar year.

    Returns dict keyed by year string (e.g., '2024') → metrics dict.
    """
    by_year: dict[str, list[dict]] = {}
    for t in trades:
        yr = str(t.get("year", "unknown"))
        by_year.setdefault(yr, []).append(t)

    return {yr: compute_metrics(yr_trades) for yr, yr_trades in sorted(by_year.items())}


# ══════════════════════════════════════════════════════════════════════
#  Hard filters
# ══════════════════════════════════════════════════════════════════════

def _passes_hard_filters(metrics: dict, thresholds: dict) -> bool:
    """
    Return True if all hard filter conditions are met.

    Hard filters (all required to pass):
      n_trades   >= thresholds["n_trades_floor"] (or "n_trades_floor_train" if former absent)
      cvar5_pct  >= thresholds["cvar5_floor_pct"]
      pf         >= thresholds["pf_floor"]

    Note: max_loss_pct is a reported diagnostic only, not a hard filter.
    """
    n_floor = thresholds.get("n_trades_floor")
    if n_floor is None:
        n_floor = thresholds.get("n_trades_floor_train", 200)
    cvar_floor = thresholds.get("cvar5_floor_pct", -8.0)
    pf_floor = thresholds.get("pf_floor", 1.0)

    if metrics.get("n_trades", 0) < n_floor:
        return False
    cvar = metrics.get("cvar5_pct")
    if cvar is None or math.isnan(cvar) or cvar < cvar_floor:
        return False
    pf = metrics.get("pf")
    if pf is None or math.isnan(pf) or pf < pf_floor:
        return False
    return True


def apply_hard_filters(
    config_ids: list[str],
    metrics_by_config: dict[str, dict],
    thresholds: dict,
) -> list[str]:
    """
    Return the subset of config_ids that pass all hard filters.
    """
    return [cid for cid in config_ids if _passes_hard_filters(metrics_by_config[cid], thresholds)]


# ══════════════════════════════════════════════════════════════════════
#  Borda ranking
# ══════════════════════════════════════════════════════════════════════

def _borda_rank_list(values: list[tuple[str, float]]) -> dict[str, int]:
    """
    Given a list of (config_id, value) pairs (higher value = better),
    assign Borda points (0 = worst, n-1 = best).  Ties share the average rank.
    Returns dict: config_id → borda_points.
    """
    if not values:
        return {}

    # Sort descending (higher value = better)
    sorted_vals = sorted(values, key=lambda x: x[1], reverse=True)
    n = len(sorted_vals)
    result: dict[str, float] = {}
    i = 0
    while i < n:
        # Find all tied entries
        j = i
        while j < n and sorted_vals[j][1] == sorted_vals[i][1]:
            j += 1
        # All from i to j-1 share the same rank
        # Borda points for ranks i..j-1 (0-indexed from worst):
        # raw positions are (n-1-i) down to (n-j) from worst
        avg_points = sum(n - 1 - k for k in range(i, j)) / (j - i)
        for k in range(i, j):
            result[sorted_vals[k][0]] = avg_points
        i = j
    return {cid: round(pts, 4) for cid, pts in result.items()}


def borda_rank(
    config_ids: list[str],
    metrics_by_config: dict[str, dict],
) -> list[str]:
    """
    Rank configs by Borda score over {capture_fraction, ev, cvar5_pct}.
    All three axes are higher-is-better.

    Tiebreaker 1: median_pct (higher is better)
    Tiebreaker 2: n_trades (higher is better)

    Returns config_ids sorted best-first.
    """
    if not config_ids:
        return []

    def _get(cid: str, key: str) -> float:
        v = metrics_by_config[cid].get(key)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return -float("inf")
        return float(v)

    # Borda over three axes
    axes = ["capture_fraction", "ev", "cvar5_pct"]
    total_borda: dict[str, float] = {cid: 0.0 for cid in config_ids}
    for axis in axes:
        vals = [(cid, _get(cid, axis)) for cid in config_ids]
        scores = _borda_rank_list(vals)
        for cid, pts in scores.items():
            total_borda[cid] += pts

    # Sort: primary = total_borda desc, tie1 = median_pct desc, tie2 = n_trades desc
    def _sort_key(cid: str):
        return (
            -total_borda[cid],
            -_get(cid, "median_pct"),
            -_get(cid, "n_trades"),
        )

    return sorted(config_ids, key=_sort_key)


# ══════════════════════════════════════════════════════════════════════
#  Full selection pipeline
# ══════════════════════════════════════════════════════════════════════

def select_winner(
    metrics_by_config: dict[str, dict],
    thresholds: dict,
) -> Optional[str]:
    """
    Hard filter → Borda rank → return winner config_id, or None if no survivor.
    """
    all_ids = list(metrics_by_config.keys())
    survivors = apply_hard_filters(all_ids, metrics_by_config, thresholds)
    if not survivors:
        return None
    ranked = borda_rank(survivors, metrics_by_config)
    return ranked[0]
