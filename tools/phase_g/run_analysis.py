"""Phase G -- Scanner Context & Time-of-Day Analysis (T1-T5).

Reads: results/phase_f/val_full/per_trade.parquet
Writes: results/phase_g/*.parquet and results/phase_g/*.json

Usage:
    python -m tools.phase_g.run_analysis
"""
from __future__ import annotations

import json
import logging
import pathlib
import sys
import time
from typing import Any

import numpy as np
import pandas as pd

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from core.filters.setup_filter import _compute_setup_signals  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_DATA_ROOT = pathlib.Path("D:/Trading Research/data")
_MIN_DIR = _DATA_ROOT / "minute"
_DAILY_DIR = _DATA_ROOT / "daily"
_MOM_EVENTS_PATH = _DATA_ROOT / "momentum_events" / "filtered_events_power_law_q05.parquet"
_PER_TRADE_PATH = _ROOT / "results" / "phase_f" / "val_full" / "per_trade.parquet"
_OUT_DIR = _ROOT / "results" / "phase_g"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
Q_THRESH = 0.65
STREAK_MIN = 15
RANDOM_SEED = 42
N_DATES = 80
N_BOOT = 1000
PHASE_F_PF = 1.9194

# Session boundaries in seconds from 4:00 AM ET (= time_of_day_sec)
_RTH_START_SEC = 5.5 * 3600   # 09:30 ET
_RTH_END_SEC = 12.0 * 3600    # 16:00 ET

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    datefmt="%H:%M:%S",
)
log = logging.getLogger("phase_g")


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
    return float(np.percentile(stats, 100 * alpha / 2)), float(np.percentile(stats, 100 * (1 - alpha / 2)))


def _session_label(tod_sec: float) -> str:
    if tod_sec < _RTH_START_SEC:
        return "pre_market"
    if tod_sec < _RTH_END_SEC:
        return "rth"
    return "post_market"


# ---------------------------------------------------------------------------
# T1 -- Sample dates
# ---------------------------------------------------------------------------

def t1_sample_dates(per_trade: pd.DataFrame) -> tuple[list[str], dict]:
    """Return 80 dates stratified by year (seed=42)."""
    log.info("T1: sampling dates")
    per_trade = per_trade.copy()
    per_trade["year"] = pd.to_datetime(per_trade["date"]).dt.year

    unique_dates = (
        per_trade.groupby(["date", "year"])
        .size()
        .reset_index()[["date", "year"]]
        .drop_duplicates()
    )

    years = sorted(unique_dates["year"].unique())
    total = len(unique_dates)
    rng = np.random.default_rng(RANDOM_SEED)

    sampled: list[str] = []
    year_alloc: dict[int, int] = {}
    remaining = N_DATES

    for i, yr in enumerate(years):
        yr_dates = unique_dates[unique_dates["year"] == yr]["date"].tolist()
        if i == len(years) - 1:
            n = remaining
        else:
            yr_frac = len(yr_dates) / total
            n = round(N_DATES * yr_frac)
        n = min(n, len(yr_dates))
        year_alloc[int(yr)] = n
        chosen = rng.choice(yr_dates, size=n, replace=False).tolist()
        sampled.extend(sorted(chosen))
        remaining -= n

    sampled = sorted(sampled)

    # T1a: confirm each date has at least 1 trade
    trade_dates = set(per_trade["date"])
    missing = [d for d in sampled if d not in trade_dates]
    assert not missing, f"Sampled dates with no trades: {missing}"

    # T1b: log year distribution
    year_dist: dict[str, int] = {}
    for d in sampled:
        yr = str(pd.Timestamp(d).year)
        year_dist[yr] = year_dist.get(yr, 0) + 1
    log.info("Year distribution: %s", year_dist)

    result = {
        "sampled_dates": sampled,
        "n_dates": len(sampled),
        "year_distribution": year_dist,
        "year_allocation": {str(k): v for k, v in year_alloc.items()},
    }
    return sampled, result


# ---------------------------------------------------------------------------
# T2 -- Build scanner leaderboard per date
# ---------------------------------------------------------------------------

def _ever_qualifies(q_tilde: np.ndarray) -> bool:
    """Return True if ticker achieves STREAK_MIN consecutive bars >= Q_THRESH
    at any point in the session (including after entry).

    Used to build the 'daily scanner' set: tickers that demonstrated
    sufficient momentum quality during the session day.
    """
    streak = 0
    for q in q_tilde:
        if q >= Q_THRESH:
            streak += 1
            if streak >= STREAK_MIN:
                return True
        else:
            streak = 0
    return False


def _filter_session_bars(bars: pd.DataFrame, date: str) -> pd.DataFrame:
    """Filter to the trading session window for date (08:00 UTC to 02:00 UTC+1)."""
    d = pd.Timestamp(date)
    start = d + pd.Timedelta(hours=8)
    end = d + pd.Timedelta(days=1, hours=2)
    mask = (bars["timestamp"] >= start) & (bars["timestamp"] < end)
    return bars[mask].reset_index(drop=True)


def _build_ticker_series(
    ticker: str, date: str, prev_close: float
) -> pd.DataFrame | None:
    """Load minute bars, run setup filter, return per-bar series.

    Returns None if the ticker never achieves the STREAK_MIN consecutive
    bars with q_tilde >= Q_THRESH ('daily qualified' check).  Only tickers
    that pass this check are included in the scanner leaderboard for the day.
    """
    path = _MIN_DIR / ticker / f"{date}.parquet"
    if not path.exists():
        return None

    raw = pd.read_parquet(path)
    bars = _filter_session_bars(raw, date)
    if len(bars) < STREAK_MIN:
        return None

    # Ensure numeric types
    for col in ["open", "high", "low", "close", "volume"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce").fillna(0.0)

    vwap = pd.to_numeric(bars.get("vwap", pd.Series(dtype=float)), errors="coerce")
    dv = np.where(
        vwap.notna() & (vwap > 0),
        vwap.values * bars["volume"].values,
        bars["close"].values * bars["volume"].values,
    ).astype(np.float64)

    try:
        _, _, _, _, _, q_tilde = _compute_setup_signals(
            bars["open"].values.astype(np.float64),
            bars["high"].values.astype(np.float64),
            bars["low"].values.astype(np.float64),
            bars["close"].values.astype(np.float64),
            bars["volume"].values.astype(np.float64),
            dv,
        )
    except Exception:
        return None

    # Daily qualified check: ticker must achieve the streak at some point
    if not _ever_qualifies(q_tilde):
        return None

    pct_change = np.where(
        prev_close > 0,
        (bars["close"].values - prev_close) / prev_close,
        np.nan,
    )

    # Store UTC nanoseconds for fast searchsorted lookup
    utc_ns = bars["timestamp"].values.astype(np.int64)

    return pd.DataFrame({
        "utc_ns": utc_ns,
        "close": bars["close"].values,
        "pct_change": pct_change,
    })


def t2_build_scanners(
    sampled_dates: list[str],
    mom_events: pd.DataFrame,
) -> tuple[dict[str, dict[str, pd.DataFrame]], dict]:
    """Build per-date, per-ticker bar series with active status."""
    log.info("T2: building scanner leaderboards for %d dates", len(sampled_dates))

    # Lookup: (ticker, date) -> prev_close
    pc_lookup: dict[tuple[str, str], float] = {}
    for _, row in mom_events.iterrows():
        if row["prev_close"] and row["prev_close"] > 0:
            pc_lookup[(row["ticker"], row["date"])] = float(row["prev_close"])

    # Co-events by date
    date_events: dict[str, list[str]] = {}
    for d in sampled_dates:
        rows = mom_events[mom_events["date"] == d]
        date_events[d] = rows["ticker"].tolist()

    scanners: dict[str, dict[str, pd.DataFrame]] = {}
    build_log: dict[str, Any] = {
        "dates_processed": 0,
        "co_events_before_filter": [],
        "active_names_after_filter": [],
        "zero_active_at_entry": [],
    }

    for date in sampled_dates:
        tickers = date_events.get(date, [])
        date_scanner: dict[str, pd.DataFrame] = {}

        for ticker in tickers:
            pc = pc_lookup.get((ticker, date))
            if pc is None or pc <= 0:
                # Fallback: try daily parquet
                dpath = _DAILY_DIR / f"{ticker}_daily.parquet"
                if dpath.exists():
                    try:
                        db = pd.read_parquet(dpath)
                        prior = db[db["date"] < date].sort_values("date")
                        if len(prior) > 0:
                            pc = float(prior.iloc[-1]["close"])
                    except Exception:
                        pass
            if pc is None or pc <= 0:
                continue

            series = _build_ticker_series(ticker, date, pc)
            if series is not None:
                date_scanner[ticker] = series

        scanners[date] = date_scanner
        build_log["dates_processed"] += 1
        build_log["co_events_before_filter"].append(len(tickers))
        # Active names: tickers that ever have is_active=True
        n_active = len(date_scanner)  # all tickers in scanner are day-qualified
        build_log["active_names_after_filter"].append(n_active)

        if build_log["dates_processed"] % 10 == 0:
            log.info("  T2 progress: %d/%d dates", build_log["dates_processed"], len(sampled_dates))

    summary = {
        "total_dates_processed": build_log["dates_processed"],
        "mean_co_events_before_filter": float(np.mean(build_log["co_events_before_filter"])),
        "mean_active_names_after_filter": float(np.mean(build_log["active_names_after_filter"])),
        "zero_active_at_entry_incidents": build_log["zero_active_at_entry"],
    }

    # Escalation checks
    n_zero = len(build_log["zero_active_at_entry"])
    mean_active = summary["mean_active_names_after_filter"]
    if n_zero > 5:
        log.error("ESCALATION: %d dates with 0 active scanner names at entry (threshold >5)", n_zero)
        log.error("Affected: %s", build_log["zero_active_at_entry"])
        raise SystemExit("T2 escalation: too many dates with no active scanner names.")
    if mean_active < 2.0:
        log.error("ESCALATION: mean active names %.2f < 2.0 -- setup filter may be over-aggressive", mean_active)
        raise SystemExit("T2 escalation: mean active names per date below threshold.")

    log.info(
        "T2 complete: %d dates, mean co-events=%.1f (before), mean active=%.1f (after)",
        build_log["dates_processed"],
        summary["mean_co_events_before_filter"],
        summary["mean_active_names_after_filter"],
    )
    return scanners, summary


# ---------------------------------------------------------------------------
# T3 -- Join scanner context to per-trade records
# ---------------------------------------------------------------------------

def _lookup_leaderboard(
    entry_ts_ns: int,
    date_scanner: dict[str, pd.DataFrame],
) -> list[dict]:
    """Return sorted leaderboard snapshot at entry_ts_ns (UTC ns).

    All tickers in date_scanner are 'daily qualified' (they achieved the
    streak at some point during the session), so no active-status check is
    needed.  Rank by pct_change from prev_close at the bar nearest to entry.
    If no bar exists at or before entry (ticker not yet trading), use 0.0.
    """
    minute_ns = (entry_ts_ns // 60_000_000_000) * 60_000_000_000

    snapshot = []
    for ticker, series in date_scanner.items():
        utc_ns = series["utc_ns"].values
        idx = np.searchsorted(utc_ns, minute_ns, side="right") - 1
        if idx < 0:
            pct = 0.0  # ticker not yet trading at entry time
        else:
            pct = float(series.iloc[idx]["pct_change"])
            if np.isnan(pct):
                pct = 0.0
        snapshot.append({
            "ticker": ticker,
            "pct_change": pct,
        })

    snapshot.sort(key=lambda x: x["pct_change"], reverse=True)
    for rank, item in enumerate(snapshot, 1):
        item["rank"] = rank
    return snapshot


def t3_join_context(
    per_trade: pd.DataFrame,
    scanners: dict[str, dict[str, pd.DataFrame]],
    sampled_dates: list[str],
    build_log_dates: dict,
) -> tuple[pd.DataFrame, dict]:
    """Join scanner leaderboard context to trades on sampled dates."""
    log.info("T3: joining scanner context")

    sampled_set = set(sampled_dates)
    trades = per_trade[per_trade["date"].isin(sampled_set)].copy()
    n_total = len(trades)
    log.info("  Trades in sampled dates: %d", n_total)

    rows = []
    n_matched = 0
    n_null_rank = 0
    n_zero_active_at_entry = 0
    null_reasons: list[dict] = []

    for _, trade in trades.iterrows():
        date = trade["date"]
        ticker = trade["ticker"]
        entry_ts_ns = int(trade["entry_ts"])

        date_scanner = scanners.get(date, {})
        snapshot = _lookup_leaderboard(entry_ts_ns, date_scanner)

        # Count active at this minute
        scanner_n = len(snapshot)
        if scanner_n == 0:
            n_zero_active_at_entry += 1

        # Find this ticker in snapshot
        ticker_entry = next((x for x in snapshot if x["ticker"] == ticker), None)

        if ticker_entry is None:
            n_null_rank += 1
            null_reasons.append({
                "ticker": ticker,
                "date": date,
                "session": trade["session_bucket"],
                "reason": "not_on_active_scanner",
            })
            scanner_rank = None
            rank_pct = None
            traded_pct_change = None
            relative_strength = None
        else:
            n_matched += 1
            scanner_rank = int(ticker_entry["rank"])
            traded_pct_change = float(ticker_entry["pct_change"])
            rank_pct = scanner_rank / scanner_n if scanner_n > 0 else None
            pct_changes = [x["pct_change"] for x in snapshot]
            scanner_median = float(np.median(pct_changes)) if pct_changes else None
            relative_strength = (
                (traded_pct_change - scanner_median) / abs(scanner_median)
                if scanner_median and abs(scanner_median) > 1e-9
                else None
            )

        pct_changes = [x["pct_change"] for x in snapshot]
        scanner_median_pct = float(np.median(pct_changes)) if pct_changes else None
        scanner_heat = float(np.percentile(pct_changes, 75)) if pct_changes else None

        rows.append({
            "ticker": ticker,
            "date": date,
            "entry_ts": entry_ts_ns,
            "pnl_pct": float(trade["pnl_pct"]),
            "exit_reason": trade["exit_reason"],
            "entry_type": trade["entry_type"],
            "session_bucket": trade["session_bucket"],
            "time_of_day_sec": float(trade["time_of_day_sec"]),
            "hold_sec": float(trade["hold_sec"]),
            "scanner_rank": scanner_rank,
            "scanner_n": scanner_n,
            "rank_pct": rank_pct,
            "traded_pct_change": traded_pct_change,
            "scanner_median_pct": scanner_median_pct,
            "scanner_heat": scanner_heat,
            "relative_strength": relative_strength,
            "full_scanner_snapshot": json.dumps(snapshot),
        })

    ctx = pd.DataFrame(rows)

    # Escalation checks
    join_rate = n_matched / n_total if n_total > 0 else 0.0
    null_rate = n_null_rank / n_total if n_total > 0 else 0.0
    log.info(
        "T3: matched=%d/%d (%.1f%%), null_rank=%d (%.1f%%)",
        n_matched, n_total, join_rate * 100,
        n_null_rank, null_rate * 100,
    )

    if join_rate < 0.90:
        log.error("ESCALATION: join rate %.1f%% < 90%%", join_rate * 100)
        log.error("Null reasons: %s", null_reasons[:20])
        raise SystemExit("T3 escalation: join rate below 90%.")
    if null_rate > 0.15:
        log.error("ESCALATION: null scanner_rank rate %.1f%% > 15%%", null_rate * 100)
        raise SystemExit("T3 escalation: too many null scanner_rank trades.")

    join_summary = {
        "n_trades_in_sampled_dates": n_total,
        "n_matched": n_matched,
        "n_null_rank": n_null_rank,
        "join_rate": round(join_rate, 4),
        "null_rate": round(null_rate, 4),
        "n_zero_active_at_entry": n_zero_active_at_entry,
        "null_breakdown": null_reasons[:50],
    }
    return ctx, join_summary


# ---------------------------------------------------------------------------
# T4 -- Time-of-day stats (full 6,004 trades)
# ---------------------------------------------------------------------------

def t4_tod_stats(per_trade: pd.DataFrame) -> pd.DataFrame:
    """Bucket all trades by 10-min interval, compute stats."""
    log.info("T4: computing TOD stats on %d trades", len(per_trade))

    df = per_trade.copy()
    df["bucket_sec"] = (df["time_of_day_sec"] // 600).astype(int) * 600
    df["session"] = df["time_of_day_sec"].apply(_session_label)

    rows = []
    for (bucket_sec, session), g in df.groupby(["bucket_sec", "session"]):
        pnl = g["pnl_pct"].values
        wins = pnl[pnl > 0]
        losses = pnl[pnl < 0]
        pf = float(wins.sum() / abs(losses.sum())) if len(losses) > 0 else float("nan")

        # Label: 04:00, 04:10, ...
        total_sec = int(bucket_sec) + 4 * 3600  # add 4 AM base
        hh = total_sec // 3600
        mm = (total_sec % 3600) // 60
        label = f"{hh:02d}:{mm:02d}"

        rows.append({
            "bucket_sec": int(bucket_sec),
            "bucket_label": label,
            "session": session,
            "n_trades": len(g),
            "win_rate": float((pnl > 0).mean()),
            "pf": pf,
            "ev": float(pnl.mean()),
            "ev_median": float(np.median(pnl)),
            "mean_hold_sec": float(g["hold_sec"].mean()),
        })

    tod = pd.DataFrame(rows).sort_values("bucket_sec").reset_index(drop=True)
    return tod


# ---------------------------------------------------------------------------
# T5 -- Scanner analysis
# ---------------------------------------------------------------------------

def t5a_rank_stats(ctx: pd.DataFrame) -> pd.DataFrame:
    """EV, PF, WR + bootstrap CI by individual scanner_rank."""
    log.info("T5a: rank stats")
    valid = ctx[ctx["scanner_rank"].notna()].copy()
    valid["scanner_rank"] = valid["scanner_rank"].astype(int)

    # Escalation: ranks 1-5 all have n_trades < 20
    top5 = valid[valid["scanner_rank"] <= 5]
    if len(top5) > 0:
        min_n = top5.groupby("scanner_rank").size().min()
        if min_n < 20:
            log.warning("Ranks 1-5 minimum n_trades=%d (< 20) -- CIs will be wide", min_n)
            if top5.groupby("scanner_rank").size().max() < 20:
                log.error("ESCALATION: all ranks 1-5 have n_trades < 20")
                raise SystemExit("T5a escalation: all ranks 1-5 below n=20.")

    rows = []
    for rank, g in valid.groupby("scanner_rank"):
        pnl = g["pnl_pct"].values
        n = len(pnl)
        ev = float(pnl.mean())
        pf = _pf(pnl)
        wr = float((pnl > 0).mean())
        ev_lo, ev_hi = _bootstrap_ci(pnl, np.mean)
        pf_lo, pf_hi = _bootstrap_ci(pnl, _pf)
        rows.append({
            "scanner_rank": int(rank),
            "n_trades": n,
            "ev": ev,
            "ev_ci_low": ev_lo,
            "ev_ci_high": ev_hi,
            "pf": pf,
            "pf_ci_low": pf_lo,
            "pf_ci_high": pf_hi,
            "win_rate": wr,
            "low_n_flag": n < 20,
        })

    return pd.DataFrame(rows).sort_values("scanner_rank").reset_index(drop=True)


def t5b_heat_bin_stats(ctx: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Stats by scanner heat quartile bins."""
    log.info("T5b: heat bin stats")
    valid = ctx[ctx["scanner_rank"].notna() & ctx["scanner_heat"].notna()].copy()

    quantiles = valid["scanner_heat"].quantile([0.25, 0.5, 0.75]).to_dict()
    q25, q50, q75 = quantiles[0.25], quantiles[0.5], quantiles[0.75]

    def heat_bin(v: float) -> str:
        if v <= q25:
            return "cold_Q1"
        if v <= q50:
            return "Q2"
        if v <= q75:
            return "Q3"
        return "hot_Q4"

    valid["heat_bin"] = valid["scanner_heat"].apply(heat_bin)
    bin_order = ["cold_Q1", "Q2", "Q3", "hot_Q4"]

    rows = []
    for b in bin_order:
        g = valid[valid["heat_bin"] == b]
        if len(g) == 0:
            continue
        pnl = g["pnl_pct"].values
        ev = float(pnl.mean())
        pf = _pf(pnl)
        wr = float((pnl > 0).mean())
        ev_lo, ev_hi = _bootstrap_ci(pnl, np.mean)
        pf_lo, pf_hi = _bootstrap_ci(pnl, _pf)
        rows.append({
            "heat_bin": b,
            "n_trades": len(g),
            "ev": ev,
            "ev_ci_low": ev_lo,
            "ev_ci_high": ev_hi,
            "pf": pf,
            "pf_ci_low": pf_lo,
            "pf_ci_high": pf_hi,
            "win_rate": wr,
            "heat_q25_bound": round(q25, 4),
            "heat_q50_bound": round(q50, 4),
            "heat_q75_bound": round(q75, 4),
        })

    bin_defs = {"q25": q25, "q50": q50, "q75": q75}
    return pd.DataFrame(rows), bin_defs


def t5c_rank_heat_interaction(
    ctx: pd.DataFrame, bin_defs: dict
) -> pd.DataFrame:
    """EV and PF for ranks 1-10 split by cold (Q1) vs hot (Q4) heat bins."""
    log.info("T5c: rank x heat interaction")
    valid = ctx[ctx["scanner_rank"].notna() & ctx["scanner_heat"].notna()].copy()
    valid["scanner_rank"] = valid["scanner_rank"].astype(int)
    q25, q75 = bin_defs["q25"], bin_defs["q75"]
    valid = valid[valid["scanner_rank"] <= 10].copy()

    rows = []
    for rank in range(1, 11):
        for heat_label, mask_fn in [
            ("cold_Q1", lambda df, q=q25: df["scanner_heat"] <= q),
            ("hot_Q4", lambda df, q=q75: df["scanner_heat"] > q),
        ]:
            g = valid[(valid["scanner_rank"] == rank) & mask_fn(valid)]
            pnl = g["pnl_pct"].values if len(g) > 0 else np.array([])
            rows.append({
                "scanner_rank": rank,
                "heat_bin": heat_label,
                "n_trades": len(g),
                "ev": float(pnl.mean()) if len(pnl) > 0 else float("nan"),
                "pf": _pf(pnl) if len(pnl) > 0 else float("nan"),
            })

    return pd.DataFrame(rows)


def t5d_exit_by_scanner_context(ctx: pd.DataFrame) -> pd.DataFrame:
    """Exit type distribution by rank (1-10) and heat bin."""
    log.info("T5d: exit type by scanner context")
    valid = ctx[ctx["scanner_rank"].notna()].copy()
    valid["scanner_rank"] = valid["scanner_rank"].astype(int)

    pop_exit_share = valid["exit_reason"].value_counts(normalize=True)
    luld_upper_pop_share = float(pop_exit_share.get("luld_upper", 0))

    rows = []
    # By rank 1-10
    for rank in range(1, 11):
        g = valid[valid["scanner_rank"] == rank]
        if len(g) == 0:
            continue
        shares = g["exit_reason"].value_counts(normalize=True)
        luld_share = float(shares.get("luld_upper", 0))
        rows.append({
            "dimension": "rank",
            "key": str(rank),
            "n_trades": len(g),
            "luld_upper_share": luld_share,
            "epg_window_close_share": float(shares.get("epg_window_close", 0)),
            "exit_d_share": float(shares.get("exit_d", 0)),
            "luld_upper_vs_pop": luld_share / luld_upper_pop_share if luld_upper_pop_share > 0 else float("nan"),
            "flag_luld_elevated": luld_share > 1.5 * luld_upper_pop_share,
        })

    # By heat bin
    q25 = ctx[ctx["scanner_heat"].notna()]["scanner_heat"].quantile(0.25)
    q50 = ctx[ctx["scanner_heat"].notna()]["scanner_heat"].quantile(0.50)
    q75 = ctx[ctx["scanner_heat"].notna()]["scanner_heat"].quantile(0.75)

    def _heat_bin(v):
        if v <= q25:
            return "cold_Q1"
        if v <= q50:
            return "Q2"
        if v <= q75:
            return "Q3"
        return "hot_Q4"

    for_heat = valid[valid["scanner_heat"].notna()].copy()
    for_heat["heat_bin"] = for_heat["scanner_heat"].apply(_heat_bin)
    for b in ["cold_Q1", "Q2", "Q3", "hot_Q4"]:
        g = for_heat[for_heat["heat_bin"] == b]
        if len(g) == 0:
            continue
        shares = g["exit_reason"].value_counts(normalize=True)
        luld_share = float(shares.get("luld_upper", 0))
        rows.append({
            "dimension": "heat_bin",
            "key": b,
            "n_trades": len(g),
            "luld_upper_share": luld_share,
            "epg_window_close_share": float(shares.get("epg_window_close", 0)),
            "exit_d_share": float(shares.get("exit_d", 0)),
            "luld_upper_vs_pop": luld_share / luld_upper_pop_share if luld_upper_pop_share > 0 else float("nan"),
            "flag_luld_elevated": luld_share > 1.5 * luld_upper_pop_share,
        })

    return pd.DataFrame(rows)


def t5e_scanner_size_stats(ctx: pd.DataFrame) -> pd.DataFrame:
    """EV, PF, WR by scanner size quartile bin."""
    log.info("T5e: scanner size stats")
    valid = ctx[ctx["scanner_rank"].notna() & ctx["scanner_n"].notna()].copy()

    quantiles = valid["scanner_n"].quantile([0.25, 0.5, 0.75]).to_dict()
    q25, q50, q75 = quantiles[0.25], quantiles[0.5], quantiles[0.75]

    def size_bin(v):
        if v <= q25:
            return f"small_Q1 (n<={q25:.0f})"
        if v <= q50:
            return f"Q2 (n<={q50:.0f})"
        if v <= q75:
            return f"Q3 (n<={q75:.0f})"
        return f"large_Q4 (n>{q75:.0f})"

    valid["size_bin"] = valid["scanner_n"].apply(size_bin)
    bin_order = [size_bin(q25), size_bin(q50), size_bin(q75), f"large_Q4 (n>{q75:.0f})"]

    rows = []
    for b in sorted(valid["size_bin"].unique(), key=lambda x: valid[valid["size_bin"] == x]["scanner_n"].mean()):
        g = valid[valid["size_bin"] == b]
        pnl = g["pnl_pct"].values
        ev_lo, ev_hi = _bootstrap_ci(pnl, np.mean)
        pf_lo, pf_hi = _bootstrap_ci(pnl, _pf)
        rows.append({
            "size_bin": b,
            "n_trades": len(g),
            "ev": float(pnl.mean()),
            "ev_ci_low": ev_lo,
            "ev_ci_high": ev_hi,
            "pf": _pf(pnl),
            "pf_ci_low": pf_lo,
            "pf_ci_high": pf_hi,
            "win_rate": float((pnl > 0).mean()),
            "mean_scanner_n": float(g["scanner_n"].mean()),
        })

    return pd.DataFrame(rows)


def t5f_multi_day_runner(
    ctx: pd.DataFrame, mom_events: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Flag multi-day runners and compute stats by group."""
    log.info("T5f: multi-day runner analysis")

    # Build set of (ticker, date) in momentum_events
    event_set: set[tuple[str, str]] = set(zip(mom_events["ticker"], mom_events["date"]))

    def is_multi_day(ticker: str, date: str) -> bool:
        d = pd.Timestamp(date)
        for delta in range(1, 6):
            prior = (d - pd.Timedelta(days=delta)).strftime("%Y-%m-%d")
            if (ticker, prior) in event_set:
                return True
        return False

    ctx = ctx.copy()
    ctx["is_multi_day_runner"] = [
        is_multi_day(row["ticker"], row["date"])
        for _, row in ctx.iterrows()
    ]

    rows = []
    for group, g in ctx.groupby("is_multi_day_runner"):
        pnl = g["pnl_pct"].values
        ev_lo, ev_hi = _bootstrap_ci(pnl, np.mean)
        pf_lo, pf_hi = _bootstrap_ci(pnl, _pf)
        rows.append({
            "group": "multi_day_runner" if group else "fresh_event",
            "n_trades": len(g),
            "ev": float(pnl.mean()),
            "ev_ci_low": ev_lo,
            "ev_ci_high": ev_hi,
            "pf": _pf(pnl),
            "pf_ci_low": pf_lo,
            "pf_ci_high": pf_hi,
            "win_rate": float((pnl > 0).mean()),
        })

    return ctx, pd.DataFrame(rows)


def t5g_entry_lag_stats(ctx: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    """Entry lag stats by bucket, using time_of_day_sec as proxy."""
    log.info("T5g: entry lag stats (using time_of_day_sec as proxy for event lag)")
    log.info("  Note: t_event not in per_trade.parquet; no equivalent field found.")
    log.info("  Source: time_of_day_sec (seconds from 04:00 ET session open)")

    df = ctx.copy()
    df["entry_lag_sec"] = df["time_of_day_sec"]

    buckets = [
        ("0-60s", 0, 60),
        ("60-300s", 60, 300),
        ("300-600s", 300, 600),
        ("600-1800s", 600, 1800),
        ("1800s+", 1800, float("inf")),
    ]

    rows = []
    for label, lo, hi in buckets:
        g = df[(df["entry_lag_sec"] >= lo) & (df["entry_lag_sec"] < hi)]
        if len(g) == 0:
            continue
        pnl = g["pnl_pct"].values
        rows.append({
            "lag_bucket": label,
            "n_trades": len(g),
            "ev": float(pnl.mean()),
            "pf": _pf(pnl),
            "win_rate": float((pnl > 0).mean()),
        })

    # Pearson correlation
    valid = df[df["entry_lag_sec"].notna() & df["pnl_pct"].notna()]
    corr = float(np.corrcoef(valid["entry_lag_sec"].values, valid["pnl_pct"].values)[0, 1])
    if abs(corr) > 0.10:
        log.warning("Entry lag correlation with PnL%% = %.4f (magnitude > 0.10 -- flagged)", corr)

    return pd.DataFrame(rows), corr


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    t_start = time.time()
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    (_OUT_DIR / "charts").mkdir(exist_ok=True)

    log.info("Loading per_trade.parquet (%s)", _PER_TRADE_PATH)
    per_trade = pd.read_parquet(_PER_TRADE_PATH)

    log.info("Loading momentum_events (%s)", _MOM_EVENTS_PATH)
    mom_events = pd.read_parquet(_MOM_EVENTS_PATH)

    # --- T1 ---
    sampled_dates, t1_result = t1_sample_dates(per_trade)
    with open(_OUT_DIR / "sampled_dates.json", "w") as f:
        json.dump(t1_result, f, indent=2)
    log.info("T1 complete: %d dates written", len(sampled_dates))

    # --- T2 ---
    scanners, t2_summary = t2_build_scanners(sampled_dates, mom_events)
    with open(_OUT_DIR / "scanner_build_log.json", "w") as f:
        json.dump(t2_summary, f, indent=2)
    log.info("T2 complete")

    # --- T3 ---
    scanner_ctx, t3_summary = t3_join_context(per_trade, scanners, sampled_dates, t2_summary)
    scanner_ctx.to_parquet(_OUT_DIR / "scanner_context.parquet", index=False)
    log.info("T3 complete: %d rows written to scanner_context.parquet", len(scanner_ctx))

    # --- T4 ---
    tod = t4_tod_stats(per_trade)
    tod.to_parquet(_OUT_DIR / "tod_stats.parquet", index=False)
    log.info("T4 complete: %d TOD buckets", len(tod))

    # --- T5 ---
    ctx = scanner_ctx  # working copy

    rank_stats = t5a_rank_stats(ctx)
    rank_stats.to_parquet(_OUT_DIR / "rank_stats.parquet", index=False)

    heat_bin_stats, bin_defs = t5b_heat_bin_stats(ctx)
    heat_bin_stats.to_parquet(_OUT_DIR / "heat_bin_stats.parquet", index=False)

    rh_interaction = t5c_rank_heat_interaction(ctx, bin_defs)
    rh_interaction.to_parquet(_OUT_DIR / "rank_heat_interaction.parquet", index=False)

    exit_ctx = t5d_exit_by_scanner_context(ctx)
    exit_ctx.to_parquet(_OUT_DIR / "exit_by_scanner_context.parquet", index=False)

    size_stats = t5e_scanner_size_stats(ctx)
    size_stats.to_parquet(_OUT_DIR / "scanner_size_stats.parquet", index=False)

    ctx_with_runner, runner_stats = t5f_multi_day_runner(ctx, mom_events)
    # Update scanner_context.parquet with is_multi_day_runner column
    ctx_with_runner.to_parquet(_OUT_DIR / "scanner_context.parquet", index=False)
    runner_stats.to_parquet(_OUT_DIR / "multi_day_runner_stats.parquet", index=False)

    lag_stats, lag_corr = t5g_entry_lag_stats(ctx_with_runner)
    lag_stats.to_parquet(_OUT_DIR / "entry_lag_stats.parquet", index=False)

    elapsed = time.time() - t_start
    log.info("Phase G analysis complete in %.0fs", elapsed)

    # Write combined metadata for reporting
    meta = {
        "t1": t1_result,
        "t2": t2_summary,
        "t3": t3_summary,
        "t5g_lag_correlation": lag_corr,
        "elapsed_sec": round(elapsed, 1),
    }
    with open(_OUT_DIR / "phase_g_meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    log.info("All outputs written to %s", _OUT_DIR)


if __name__ == "__main__":
    main()
