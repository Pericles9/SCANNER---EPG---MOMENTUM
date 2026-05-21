"""Phase G v2 -- Momentum-Weighted Scanner Quartile Reanalysis (T1 + T2).

Reads: results/phase_g/scanner_context.parquet (stored full_scanner_snapshot)
Writes: results/phase_g_v2/*.parquet, results/phase_g_v2/v2_build_log.json

Usage:
    python -m tools.phase_g_v2.run_v2
"""
from __future__ import annotations

import json
import logging
import pathlib
import time
from typing import Any

import numpy as np
import pandas as pd

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SRC_PARQUET = _ROOT / "results" / "phase_g" / "scanner_context.parquet"
_OUT_DIR = _ROOT / "results" / "phase_g_v2"

PHASE_F_PF = 1.9194
N_BOOT = 1000
RANDOM_SEED = 42

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    datefmt="%H:%M:%S",
)
log = logging.getLogger("phase_g_v2")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pf(pnl: np.ndarray) -> float:
    wins = pnl[pnl > 0].sum()
    losses = abs(pnl[pnl < 0].sum())
    return float(wins / losses) if losses > 0 else float("nan")


def _bootstrap_ci(
    pnl: np.ndarray,
    stat_fn,
    n_boot: int = N_BOOT,
    seed: int = RANDOM_SEED,
    alpha: float = 0.05,
) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    stats = []
    for _ in range(n_boot):
        s = rng.choice(pnl, size=len(pnl), replace=True)
        v = stat_fn(s)
        if not np.isnan(v):
            stats.append(v)
    if not stats:
        return float("nan"), float("nan")
    lo = float(np.percentile(stats, 100 * alpha / 2))
    hi = float(np.percentile(stats, 100 * (1 - alpha / 2)))
    return lo, hi


# ---------------------------------------------------------------------------
# T1 -- Momentum-weighted quartile computation
# ---------------------------------------------------------------------------

def _assign_momentum_quartile(snapshot: list[dict], traded_ticker: str) -> int | None:
    """Assign momentum-weighted quartile to traded_ticker within snapshot.

    Returns 1-4 or None if total_momentum <= 0 or ticker not found.
    """
    if not snapshot:
        return None

    # Sort descending by pct_change
    names = sorted(snapshot, key=lambda x: x["pct_change"], reverse=True)
    total = sum(x["pct_change"] for x in names)

    if total <= 0:
        return None  # all flat or negative; quartile undefined

    threshold = total / 4.0
    running = 0.0
    quartile = 1
    quartile_map: dict[str, int] = {}

    for item in names:
        running += item["pct_change"]
        quartile_map[item["ticker"]] = quartile
        # Advance quartile boundary
        while quartile < 4 and running >= threshold * quartile:
            quartile += 1

    return quartile_map.get(traded_ticker)


def t1_compute_quartile(ctx: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    log.info("T1: computing scanner_quartile for %d rows", len(ctx))

    results: list[int | None] = []
    null_reasons: list[dict] = []
    n_single_name = 0
    n_null_total_momentum = 0
    n_null_not_found = 0

    for idx, row in ctx.iterrows():
        scanner_rank = row.get("scanner_rank")
        scanner_n = row.get("scanner_n", 0)
        ticker = row["ticker"]

        # Null scanner_rank means ticker never made the scanner — propagate null
        if pd.isna(scanner_rank):
            results.append(None)
            null_reasons.append({"idx": idx, "reason": "null_scanner_rank"})
            continue

        # Single-name scanner: always Q1
        if int(scanner_n) == 1:
            results.append(1)
            n_single_name += 1
            continue

        raw_snap = row.get("full_scanner_snapshot")
        if not raw_snap or pd.isna(raw_snap) if not isinstance(raw_snap, str) else False:
            results.append(None)
            null_reasons.append({"idx": idx, "reason": "null_snapshot"})
            continue

        try:
            snapshot = json.loads(raw_snap)
        except Exception:
            results.append(None)
            null_reasons.append({"idx": idx, "reason": "json_parse_error"})
            continue

        q = _assign_momentum_quartile(snapshot, ticker)

        if q is None:
            total = sum(x["pct_change"] for x in snapshot) if snapshot else 0
            if total <= 0:
                n_null_total_momentum += 1
                reason = "total_momentum_nonpositive"
            else:
                n_null_not_found += 1
                reason = "ticker_not_in_snapshot"
            results.append(None)
            null_reasons.append({"idx": idx, "ticker": ticker, "reason": reason})
        else:
            results.append(q)

    ctx = ctx.copy()
    ctx["scanner_quartile"] = results

    # Escalation checks (exclude null_scanner_rank rows from rate)
    valid_rows = ctx[ctx["scanner_rank"].notna()]
    null_count = valid_rows["scanner_quartile"].isna().sum()
    null_rate = null_count / len(valid_rows) if len(valid_rows) > 0 else 0.0

    log.info(
        "T1: total=%d, null_scanner_rank=%d, single_name=%d, "
        "null_total_momentum=%d, null_not_found=%d",
        len(ctx),
        len([r for r in null_reasons if r["reason"] == "null_scanner_rank"]),
        n_single_name,
        n_null_total_momentum,
        n_null_not_found,
    )

    q_dist: dict[str, int] = {}
    for q_val in [1, 2, 3, 4]:
        q_dist[f"Q{q_val}"] = int((ctx["scanner_quartile"] == q_val).sum())

    log.info("Quartile distribution: %s", q_dist)

    # Escalation: null rate > 10%
    if null_rate > 0.10:
        log.error(
            "ESCALATION: null scanner_quartile rate %.1f%% > 10%% -- breakdown: %s",
            null_rate * 100,
            null_reasons[:20],
        )
        raise SystemExit("T1 escalation: null_quartile_rate > 10%.")

    # Escalation: any quartile with 0 trades
    for q_val, count in q_dist.items():
        if count == 0:
            log.error("ESCALATION: quartile %s has 0 assigned trades", q_val)
            raise SystemExit(f"T1 escalation: quartile {q_val} has 0 trades.")

    build_log: dict[str, Any] = {
        "total_rows": len(ctx),
        "n_null_scanner_rank": len([r for r in null_reasons if r["reason"] == "null_scanner_rank"]),
        "n_single_name": n_single_name,
        "n_null_total_momentum": n_null_total_momentum,
        "n_null_not_found": n_null_not_found,
        "null_rate_valid_rows": round(null_rate, 4),
        "quartile_distribution": q_dist,
        "null_reasons_sample": null_reasons[:50],
    }
    return ctx, build_log


# ---------------------------------------------------------------------------
# T2 -- Reanalyse with scanner_quartile
# ---------------------------------------------------------------------------

def t2a_quartile_stats(ctx: pd.DataFrame) -> pd.DataFrame:
    log.info("T2a: quartile stats")
    valid = ctx[ctx["scanner_quartile"].notna()].copy()
    valid["scanner_quartile"] = valid["scanner_quartile"].astype(int)

    rows = []
    for q in [1, 2, 3, 4]:
        g = valid[valid["scanner_quartile"] == q]
        pnl = g["pnl_pct"].values
        n = len(pnl)
        ev = float(pnl.mean())
        pf = _pf(pnl)
        wr = float((pnl > 0).mean())
        ev_lo, ev_hi = _bootstrap_ci(pnl, np.mean)
        pf_lo, pf_hi = _bootstrap_ci(pnl, _pf)
        rows.append({
            "scanner_quartile": q,
            "n_trades": n,
            "ev": ev,
            "ev_ci_low": ev_lo,
            "ev_ci_high": ev_hi,
            "pf": pf,
            "pf_ci_low": pf_lo,
            "pf_ci_high": pf_hi,
            "win_rate": wr,
        })
    return pd.DataFrame(rows)


def t2b_rank_quartile_interaction(ctx: pd.DataFrame) -> pd.DataFrame:
    log.info("T2b: rank x quartile interaction")
    valid = ctx[ctx["scanner_quartile"].notna() & ctx["scanner_rank"].notna()].copy()
    valid["scanner_quartile"] = valid["scanner_quartile"].astype(int)
    valid["scanner_rank"] = valid["scanner_rank"].astype(int)
    valid = valid[valid["scanner_rank"] <= 10]

    rows = []
    for rank in range(1, 11):
        for q in [1, 4]:
            g = valid[(valid["scanner_rank"] == rank) & (valid["scanner_quartile"] == q)]
            pnl = g["pnl_pct"].values if len(g) > 0 else np.array([])
            rows.append({
                "scanner_rank": rank,
                "scanner_quartile": q,
                "n_trades": len(g),
                "ev": float(pnl.mean()) if len(pnl) > 0 else float("nan"),
                "pf": _pf(pnl) if len(pnl) > 0 else float("nan"),
                "low_n_flag": len(g) < 20,
            })
    return pd.DataFrame(rows)


def t2c_exit_by_quartile(ctx: pd.DataFrame) -> pd.DataFrame:
    log.info("T2c: exit type by scanner_quartile")
    valid = ctx[ctx["scanner_quartile"].notna()].copy()
    valid["scanner_quartile"] = valid["scanner_quartile"].astype(int)

    pop_exit_share = valid["exit_reason"].value_counts(normalize=True)
    luld_pop = float(pop_exit_share.get("luld_upper", 0))

    rows = []
    for q in [1, 2, 3, 4]:
        g = valid[valid["scanner_quartile"] == q]
        if len(g) == 0:
            continue
        shares = g["exit_reason"].value_counts(normalize=True)
        luld_share = float(shares.get("luld_upper", 0))
        rows.append({
            "scanner_quartile": q,
            "n_trades": len(g),
            "luld_upper_share": luld_share,
            "epg_window_close_share": float(shares.get("epg_window_close", 0)),
            "exit_d_share": float(shares.get("exit_d", 0)),
            "luld_upper_vs_pop": luld_share / luld_pop if luld_pop > 0 else float("nan"),
            "flag_luld_elevated": luld_share > 1.5 * luld_pop,
        })
    return pd.DataFrame(rows)


def t2d_scanner_n1_isolation(ctx: pd.DataFrame) -> pd.DataFrame:
    log.info("T2d: scanner_n=1 isolation")
    valid = ctx[ctx["scanner_rank"].notna()].copy()
    valid["scanner_rank"] = valid["scanner_rank"].astype(int)

    rows = []
    for group_label, mask in [
        ("scanner_n=1", valid["scanner_n"] == 1),
        ("scanner_n>1", valid["scanner_n"] > 1),
    ]:
        g = valid[mask]
        pnl = g["pnl_pct"].values
        rows.append({
            "group": group_label,
            "subset": "all_ranks",
            "n_trades": len(g),
            "ev": float(pnl.mean()) if len(pnl) > 0 else float("nan"),
            "pf": _pf(pnl) if len(pnl) > 0 else float("nan"),
            "win_rate": float((pnl > 0).mean()) if len(pnl) > 0 else float("nan"),
        })
        # Rank 1 only
        r1 = g[g["scanner_rank"] == 1]
        pnl_r1 = r1["pnl_pct"].values
        rows.append({
            "group": group_label,
            "subset": "rank_1_only",
            "n_trades": len(r1),
            "ev": float(pnl_r1.mean()) if len(pnl_r1) > 0 else float("nan"),
            "pf": _pf(pnl_r1) if len(pnl_r1) > 0 else float("nan"),
            "win_rate": float((pnl_r1 > 0).mean()) if len(pnl_r1) > 0 else float("nan"),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    t_start = time.time()
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    (_OUT_DIR / "charts").mkdir(exist_ok=True)

    if not _SRC_PARQUET.exists():
        log.error("Source not found: %s -- run tools.phase_g.run_analysis first", _SRC_PARQUET)
        raise SystemExit(1)

    log.info("Loading %s", _SRC_PARQUET)
    ctx = pd.read_parquet(_SRC_PARQUET)
    log.info("Loaded %d rows", len(ctx))

    # T1
    ctx_v2, build_log = t1_compute_quartile(ctx)
    ctx_v2.to_parquet(_OUT_DIR / "scanner_context_v2.parquet", index=False)
    with open(_OUT_DIR / "v2_build_log.json", "w") as f:
        json.dump(build_log, f, indent=2)
    log.info("T1 complete: scanner_context_v2.parquet written")

    # T2
    quartile_stats = t2a_quartile_stats(ctx_v2)
    quartile_stats.to_parquet(_OUT_DIR / "quartile_stats.parquet", index=False)

    rank_quartile = t2b_rank_quartile_interaction(ctx_v2)
    rank_quartile.to_parquet(_OUT_DIR / "rank_quartile_interaction.parquet", index=False)

    exit_by_q = t2c_exit_by_quartile(ctx_v2)
    exit_by_q.to_parquet(_OUT_DIR / "exit_by_quartile.parquet", index=False)

    n1_isolation = t2d_scanner_n1_isolation(ctx_v2)
    n1_isolation.to_parquet(_OUT_DIR / "scanner_n1_isolation.parquet", index=False)

    elapsed = time.time() - t_start
    log.info("Phase G v2 analysis complete in %.1fs", elapsed)

    meta = {
        "build_log": build_log,
        "elapsed_sec": round(elapsed, 1),
    }
    with open(_OUT_DIR / "phase_g_v2_meta.json", "w") as f:
        json.dump(meta, f, indent=2)
    log.info("All outputs written to %s", _OUT_DIR)


if __name__ == "__main__":
    main()
