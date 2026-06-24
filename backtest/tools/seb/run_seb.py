"""
Phase SEB — Scanner Entry Backtest CLI.

Measures scanner → setup filter → first-bar-above-VWAP entries
on their forward behavior, exit-agnostic.

Usage:
  # Tier 1 only (catalog):
  python backtest/tools/seb/run_seb.py --no-tier0

  # Tier 0 from DB + Tier 1:
  python backtest/tools/seb/run_seb.py --tier0-db-url postgresql://user:pw@localhost/live

  # Tier 0 from pre-exported JSON + Tier 1:
  python backtest/tools/seb/run_seb.py --tier0-json snapshots.json

  # Filter to specific dates:
  python backtest/tools/seb/run_seb.py --dates 2026-05-01 2026-05-02 --no-tier0

Outputs:
  results/seb/entries.parquet   one row per candidate (ticker, date)
  results/seb/seb_report.md     comparison report (Gate D: both Ns always shown)
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Add backtest/ (for data.*, setup_filter) and project root (for Numba cache
# reconstruction, which needs `import backtest` to resolve) to sys.path.
_REPO_ROOT = Path(__file__).resolve().parents[2]      # …/backtest
_PROJECT_ROOT = _REPO_ROOT.parent                     # …/scanner-epg-momentum
for _p in (str(_PROJECT_ROOT), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd  # noqa: E402

from tools.seb.feed import Tier0Feed, Tier1Feed  # noqa: E402
from tools.seb.simulator import simulate_session  # noqa: E402

log = logging.getLogger(__name__)

# ── Defaults ────────────────────────────────────────────────────────────
DEFAULT_POLL_INTERVAL_S = 30.0    # fallback if config unreadable
DEFAULT_MIN_MOM = 50.0
DEFAULT_RESULTS_DIR = _REPO_ROOT / "results" / "seb"
RUNNER_THRESHOLD_PCT_LABEL = 5.0  # label only; not a decision input


def _read_poll_interval() -> float:
    """Read poll_interval_s from live/strategy.json. Fallback to DEFAULT_POLL_INTERVAL_S."""
    try:
        cfg_path = _REPO_ROOT.parent / "live" / "strategy.json"
        with open(cfg_path) as f:
            raw = json.load(f)
        interval = int(raw["scanner"]["poll_interval_s"])
        log.info("Poll interval from live/strategy.json: %ds", interval)
        return float(interval)
    except Exception as exc:
        log.warning(
            "Cannot read poll_interval_s from strategy.json (%s) — using %.0fs fallback",
            exc, DEFAULT_POLL_INTERVAL_S,
        )
        return DEFAULT_POLL_INTERVAL_S


def _run_tier(feed, poll_interval_s: float, label: str) -> list[dict]:
    """Iterate a feed and simulate each session. Returns list of result dicts."""
    results = []
    n_sessions = 0
    n_entries = 0
    for spec in feed.iter_sessions():
        n_sessions += 1
        rec = simulate_session(spec, poll_interval_s=poll_interval_s)
        results.append(rec)
        if rec.get("no_entry_reason") is None:
            n_entries += 1
        if n_sessions % 50 == 0:
            log.info("%s: %d sessions processed, %d entries so far", label, n_sessions, n_entries)
    log.info("%s: done. %d sessions -> %d entries", label, n_sessions, n_entries)
    return results


def _build_df(records: list[dict]) -> pd.DataFrame:
    """Build entries DataFrame from raw records, filling missing columns with NaN."""
    if not records:
        return pd.DataFrame()
    return pd.DataFrame(records)


def _runner_rate(df: pd.DataFrame, tier: str) -> tuple[int, int, float]:
    """Return (n_entries, n_runners, runner_rate) for a given tier."""
    sub = df[(df["tier"] == tier) & (df["no_entry_reason"].isna())]
    n_entries = len(sub)
    if n_entries == 0:
        return 0, 0, float("nan")
    n_runners = int(sub["is_runner"].sum()) if "is_runner" in sub.columns else 0
    return n_entries, n_runners, n_runners / n_entries


def _gate_c_check(df: pd.DataFrame) -> None:
    """Gate C: print Tier 0 candidate and entry counts for manual count sanity check.

    Cannot validate automatically against live orders table — prints for human review.
    """
    if df.empty or "tier" not in df.columns:
        return
    t0 = df[df["tier"] == "tier0"]
    n_cands = len(t0)
    n_entries = int(t0["no_entry_reason"].isna().sum())
    no_entry_counts = (
        t0[t0["no_entry_reason"].notna()]["no_entry_reason"]
        .value_counts()
        .to_dict()
    ) if n_cands > 0 else {}
    print(
        f"\n[Gate C] Tier 0 candidates: {n_cands}  entries: {n_entries}  "
        f"no-entry breakdown: {no_entry_counts}"
    )
    print(
        "  -> Compare Tier 0 entry count against actual live entries in the DB.\n"
        "     Each difference must be explained before trusting Tier 0 runner rate.\n"
    )


def _generate_report(df: pd.DataFrame, poll_interval_s: float, out_path: Path) -> None:
    """Generate seb_report.md (Gate D: both Tier 0 and Tier 1 n always shown)."""
    lines = []
    lines.append("# Phase SEB — Scanner Entry Backtest Report")
    lines.append(f"\n_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}  "
                 f"Poll interval: {poll_interval_s:.0f}s  "
                 f"Runner threshold: MFE(30m) ≥ {RUNNER_THRESHOLD_PCT_LABEL:.0f}% (UNVALIDATED)_\n")

    if df.empty:
        lines.append("No data — both tiers empty.\n")
        out_path.write_text("\n".join(lines))
        return

    # ── Headline numbers (Gate D: never Tier 1 without Tier 0 counterpart) ──
    t0_entries, t0_runners, t0_rate = _runner_rate(df, "tier0")
    t1_entries, t1_runners, t1_rate = _runner_rate(df, "tier1")
    t0_cands = int((df["tier"] == "tier0").sum())
    t1_cands = int((df["tier"] == "tier1").sum())

    lines.append("## Headline Gap")
    lines.append("")
    lines.append(f"| Metric | Tier 0 (live) | Tier 1 (catalog) | Gap (T1−T0) |")
    lines.append(f"|--------|--------------|-----------------|-------------|")
    lines.append(
        f"| Candidates | {t0_cands} | {t1_cands} | — |"
    )
    lines.append(
        f"| Entries | {t0_entries} | {t1_entries} | {t1_entries - t0_entries:+d} |"
    )

    def _fmt_rate(r):
        return f"{r*100:.1f}%" if not math.isnan(r) else "n/a"

    gap_str = (
        f"{(t1_rate - t0_rate)*100:+.1f}pp"
        if not (math.isnan(t0_rate) or math.isnan(t1_rate)) else "n/a"
    )
    lines.append(
        f"| Runner rate | {_fmt_rate(t0_rate)} ({t0_runners}/{t0_entries}) "
        f"| {_fmt_rate(t1_rate)} ({t1_runners}/{t1_entries}) "
        f"| {gap_str} |"
    )
    lines.append("")
    lines.append(
        "> **Interpretation:** The T1−T0 gap in runner rate is the estimated "
        "selection-bias inflation from using the catalog as the universe. "
        "Tier 0 is the honest sample; Tier 1 is mechanically larger. "
        "Always read Tier 1 numbers relative to Tier 0."
    )

    # ── No-entry breakdown ──────────────────────────────────────────────
    lines.append("\n## No-Entry Breakdown\n")
    for tier_label, tier_key in [("Tier 0 (live)", "tier0"), ("Tier 1 (catalog)", "tier1")]:
        sub = df[df["tier"] == tier_key]
        if sub.empty:
            continue
        lines.append(f"### {tier_label}\n")
        ne = sub[sub["no_entry_reason"].notna()]
        if ne.empty:
            lines.append("All candidates produced entries.\n")
        else:
            counts = ne["no_entry_reason"].value_counts()
            lines.append("| Reason | Count |")
            lines.append("|--------|-------|")
            for reason, cnt in counts.items():
                lines.append(f"| {reason} | {cnt} |")
            lines.append("")

    # ── Entry distribution by session_bucket ────────────────────────────
    entries = df[df["no_entry_reason"].isna()]
    if not entries.empty and "session_bucket" in entries.columns:
        lines.append("\n## Entry Distribution by Session Bucket\n")
        for tier_label, tier_key in [("Tier 0", "tier0"), ("Tier 1", "tier1")]:
            sub = entries[entries["tier"] == tier_key]
            if sub.empty:
                continue
            lines.append(f"### {tier_label}\n")
            bc = sub["session_bucket"].value_counts()
            lines.append("| Bucket | Entries | Runner Rate |")
            lines.append("|--------|---------|-------------|")
            for bk, cnt in bc.items():
                bk_sub = sub[sub["session_bucket"] == bk]
                rr = bk_sub["is_runner"].mean() if "is_runner" in bk_sub.columns else float("nan")
                lines.append(f"| {bk} | {cnt} | {_fmt_rate(rr)} |")
            lines.append("")

    # ── MFE / return summary ─────────────────────────────────────────────
    if not entries.empty:
        lines.append("\n## Forward Return Summary (entries only)\n")
        lines.append("| Metric | Tier 0 median | Tier 1 median |")
        lines.append("|--------|--------------|--------------|")
        for col in ["mfe_5m", "mfe_15m", "mfe_30m", "ret_30m", "eod_ret"]:
            if col not in entries.columns:
                continue
            t0_med = entries[entries["tier"] == "tier0"][col].median()
            t1_med = entries[entries["tier"] == "tier1"][col].median()

            def _pct(v):
                return f"{v*100:.2f}%" if not math.isnan(v) else "n/a"

            lines.append(f"| {col} | {_pct(t0_med)} | {_pct(t1_med)} |")

    # ── Slippage ─────────────────────────────────────────────────────────
    if not entries.empty and "slippage_pct" in entries.columns:
        lines.append("\n## Entry Slippage (bar close → first tick after)\n")
        for tier_label, tier_key in [("Tier 0", "tier0"), ("Tier 1", "tier1")]:
            sub = entries[entries["tier"] == tier_key]["slippage_pct"]
            if sub.empty:
                continue
            lines.append(
                f"- **{tier_label}**: median={sub.median()*100:.3f}%  "
                f"mean={sub.mean()*100:.3f}%  "
                f"p95={sub.quantile(0.95)*100:.3f}%"
            )
        lines.append("")

    # ── Setup filter signal quality ───────────────────────────────────────
    if not entries.empty and "sf_weakest" in entries.columns:
        lines.append("\n## Setup Filter — Weakest Signal at Entry\n")
        for tier_label, tier_key in [("Tier 0", "tier0"), ("Tier 1", "tier1")]:
            sub = entries[entries["tier"] == tier_key]
            if sub.empty:
                continue
            wk = sub["sf_weakest"].value_counts()
            lines.append(f"**{tier_label}**: " + "  ".join(f"{s}={c}" for s, c in wk.items()))
        lines.append("")

    # ── Footer ───────────────────────────────────────────────────────────
    lines.append("\n---")
    lines.append(
        "_Phase SEB is a read-only research harness. "
        "No live tables were modified. "
        "Runner threshold (MFE 30m ≥ 5%) is an UNVALIDATED HEURISTIC._"
    )

    out_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Report written to %s", out_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase SEB — Scanner Entry Backtest"
    )
    # Tier 0 source
    tier0_grp = parser.add_mutually_exclusive_group()
    tier0_grp.add_argument(
        "--tier0-db-url",
        metavar="URL",
        help="PostgreSQL URL for scanner_snapshots (e.g. postgresql://user:pw@localhost/live)",
    )
    tier0_grp.add_argument(
        "--tier0-json",
        metavar="PATH",
        type=Path,
        help="Pre-exported scanner_snapshots JSON file path",
    )
    parser.add_argument(
        "--no-tier0",
        action="store_true",
        help="Skip Tier 0 (use when scanner_snapshots are unavailable)",
    )
    parser.add_argument(
        "--no-tier1",
        action="store_true",
        help="Skip Tier 1 (run Tier 0 only)",
    )
    parser.add_argument(
        "--min-mom",
        type=float,
        default=DEFAULT_MIN_MOM,
        help=f"Min catalog momentum %% for Tier 1 (default {DEFAULT_MIN_MOM})",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=3,
        help="Tier 1 calendar window around each event (default ±3 days)",
    )
    parser.add_argument(
        "--dates",
        nargs="+",
        metavar="YYYY-MM-DD",
        help="Restrict to specific session dates (applies to both tiers)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        help="Override poll interval in seconds (default: read from live/strategy.json)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_RESULTS_DIR,
        help=f"Output directory (default: {DEFAULT_RESULTS_DIR})",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="DEBUG logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # ── Validate inputs ──────────────────────────────────────────────────
    if args.no_tier0 and args.no_tier1:
        parser.error("Cannot specify both --no-tier0 and --no-tier1")

    need_tier0_source = not args.no_tier0
    has_tier0_source = args.tier0_db_url or args.tier0_json
    if need_tier0_source and not has_tier0_source:
        log.warning(
            "No Tier 0 source specified (--tier0-db-url or --tier0-json). "
            "Running Tier 1 only. Use --no-tier0 to suppress this warning."
        )
        args.no_tier0 = True

    # ── Poll interval ────────────────────────────────────────────────────
    poll_interval_s = args.poll_interval or _read_poll_interval()
    log.info("Using poll interval: %.0fs", poll_interval_s)

    # ── Run tiers ────────────────────────────────────────────────────────
    all_records: list[dict] = []

    if not args.no_tier0:
        feed0 = Tier0Feed(
            db_url=args.tier0_db_url,
            snapshots_json_path=args.tier0_json,
            session_dates=args.dates,
        )
        log.info("=== Running Tier 0 (live ground truth) ===")
        t0_records = _run_tier(feed0, poll_interval_s, "Tier0")
        all_records.extend(t0_records)

    if not args.no_tier1:
        feed1 = Tier1Feed(min_mom=args.min_mom, window_days=args.window_days)
        log.info("=== Running Tier 1 (catalog) ===")
        t1_records = _run_tier(feed1, poll_interval_s, "Tier1")
        all_records.extend(t1_records)

    if not all_records:
        log.warning("No sessions produced — check feed inputs")
        return

    # ── Build DataFrame ──────────────────────────────────────────────────
    df = _build_df(all_records)

    # ── Gate C: Tier 0 count sanity check ───────────────────────────────
    _gate_c_check(df)

    # ── Console headline (Gate D: both Ns required) ──────────────────────
    t0_n = int((df["tier"] == "tier0").sum()) if "tier" in df.columns else 0
    t1_n = int((df["tier"] == "tier1").sum()) if "tier" in df.columns else 0

    entries = df[df["no_entry_reason"].isna()] if "no_entry_reason" in df.columns else df
    t0_entries_n = int((entries.get("tier", pd.Series(dtype=str)) == "tier0").sum())
    t1_entries_n = int((entries.get("tier", pd.Series(dtype=str)) == "tier1").sum())

    t0_rr = float("nan")
    t1_rr = float("nan")
    if t0_entries_n > 0 and "is_runner" in entries.columns:
        t0_rr = float(entries[entries["tier"] == "tier0"]["is_runner"].mean())
    if t1_entries_n > 0 and "is_runner" in entries.columns:
        t1_rr = float(entries[entries["tier"] == "tier1"]["is_runner"].mean())

    def _fmt(r):
        return f"{r*100:.1f}%" if not math.isnan(r) else "n/a"

    gap_pp = (t1_rr - t0_rr) * 100 if not (math.isnan(t0_rr) or math.isnan(t1_rr)) else float("nan")
    gap_str = f"{gap_pp:+.1f}pp" if not math.isnan(gap_pp) else "n/a"

    print(
        f"\n[SEB] Tier0 n={t0_n} entries={t0_entries_n} runner={_fmt(t0_rr)} | "
        f"Tier1 n={t1_n} entries={t1_entries_n} runner={_fmt(t1_rr)} | "
        f"gap={gap_str}\n"
    )

    # ── Write outputs ────────────────────────────────────────────────────
    args.out_dir.mkdir(parents=True, exist_ok=True)

    parquet_path = args.out_dir / "entries.parquet"
    df.to_parquet(str(parquet_path), index=False)
    log.info("Wrote %s (%d rows)", parquet_path, len(df))

    report_path = args.out_dir / "seb_report.md"
    _generate_report(df, poll_interval_s, report_path)

    log.info("Phase SEB complete. Results in %s", args.out_dir)


if __name__ == "__main__":
    main()
