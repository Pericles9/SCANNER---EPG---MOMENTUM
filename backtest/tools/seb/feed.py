"""
Phase SEB — Universe feed interface and implementations.

UniverseFeed  ABC defining iter_sessions().
SessionSpec   Per-session metadata passed to the simulator.
Tier1Feed     Catalog-based feed (7-day window around each event).
Tier0Feed     Live ground truth from scanner_snapshots + session parquets.
"""
from __future__ import annotations

import json
import logging
import sys
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

import numpy as np
import pyarrow.parquet as pq

# Add backtest/ (for data.*, setup_filter) and project root (for Numba cache
# reconstruction, which needs `import backtest` to resolve) to sys.path.
_REPO_ROOT = Path(__file__).resolve().parents[2]      # …/backtest
_PROJECT_ROOT = _REPO_ROOT.parent                     # …/scanner-epg-momentum
for _p in (str(_PROJECT_ROOT), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from data.schemas.mom_db import FILTERED_DIR, NS_PER_SECOND  # noqa: E402
from data.loaders.trades import parse_event_dir, list_events  # noqa: E402

log = logging.getLogger(__name__)

# Calendar window around each catalog event day (T-N through T+N).
TIER1_WINDOW_DAYS = 3

# Pre-built index: (ticker, date) → event directory Path.
# Scanned once at import time so _find_event_dir is an O(1) dict lookup
# instead of a per-call filesystem glob (~29ms each on Windows).
_AVAILABLE_DIRS: dict[tuple[str, str], Path] = {}
if FILTERED_DIR.exists():
    for _d in FILTERED_DIR.iterdir():
        if _d.is_dir():
            _parsed = parse_event_dir(_d.name)
            if _parsed is not None and _parsed["date"] is not None:
                _AVAILABLE_DIRS[(_parsed["ticker"], _parsed["date"])] = _d
    log.debug("SEB feed: indexed %d event directories from %s", len(_AVAILABLE_DIRS), FILTERED_DIR)


@dataclass
class SessionSpec:
    """Metadata for one (ticker, date) session passed to the simulator."""
    tier: str                              # "tier0" | "tier1"
    ticker: str
    date: str                              # "YYYY-MM-DD"
    mom_pct: float                         # catalog MOM %. float("nan") if unknown.
    is_event_day: bool                     # True if this is the catalog event day
    scanner_quartile: Optional[int] = None
    # Tier 0 only: sorted [(snapshot_ns, pct_change_fraction), ...]
    # All entries already have pct_change_fraction >= 0.30 (scanner pre-filters).
    recorded_polls: Optional[list] = None


# ── Tick data loader ────────────────────────────────────────────────────

def _find_event_dir(ticker: str, date: str) -> Optional[Path]:
    """Return the FILTERED_DIR subdirectory for (ticker, date), or None.

    Uses the pre-built _AVAILABLE_DIRS index for an O(1) lookup instead of
    a per-call filesystem glob.
    """
    return _AVAILABLE_DIRS.get((ticker, date))


def load_ticks_for_session(
    ticker: str,
    date: str,
) -> Optional[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Load sorted (timestamps_ns, prices, sizes) from the filtered event parquet.

    Returns None if no parquet exists for (ticker, date).
    """
    ev_dir = _AVAILABLE_DIRS.get((ticker, date))
    if ev_dir is None:
        return None
    parquet_path = ev_dir / "trades.parquet"
    try:
        table = pq.read_table(str(parquet_path), columns=["sip_timestamp", "price", "size"])
    except Exception as exc:
        log.warning("Cannot read %s: %s", parquet_path, exc)
        return None

    ts = table.column("sip_timestamp").to_numpy().astype(np.int64)
    prices = table.column("price").to_numpy().astype(np.float64)
    sizes = table.column("size").to_numpy().astype(np.int64)

    if len(ts) == 0:
        return None

    idx = np.argsort(ts)
    return ts[idx], prices[idx], sizes[idx]


def _infer_mom_pct(ticker: str, date: str) -> float:
    """Parse mom_pct from the event directory name for (ticker, date). NaN if absent."""
    ev_dir = _AVAILABLE_DIRS.get((ticker, date))
    if ev_dir is None:
        return float("nan")
    parsed = parse_event_dir(ev_dir.name)
    if parsed is None:
        return float("nan")
    return float(parsed.get("mom_pct", float("nan")))


# ── Abstract feed interface ─────────────────────────────────────────────

class UniverseFeed(ABC):
    """Abstract base for universe feeds.  Each implementation yields SessionSpec objects."""

    @abstractmethod
    def iter_sessions(self) -> Iterator[SessionSpec]:
        """Yield one SessionSpec per (ticker, date) candidate session."""
        ...


# ── Tier 1: catalog feed ────────────────────────────────────────────────

class Tier1Feed(UniverseFeed):
    """Catalog-based feed.

    For each event in the filtered catalog, yields calendar days
    T-WINDOW through T+WINDOW where tick data exists in FILTERED_DIR.
    Deduplicates (ticker, date) pairs across overlapping event windows.
    """

    def __init__(self, min_mom: float = 50.0, window_days: int = TIER1_WINDOW_DAYS):
        self.min_mom = min_mom
        self.window_days = window_days

    def iter_sessions(self) -> Iterator[SessionSpec]:
        import datetime

        events = list_events(min_mom=self.min_mom, require_date=True)
        log.info("Tier1Feed: %d catalog events (min_mom=%.0f%%)", len(events), self.min_mom)

        seen: set[tuple[str, str]] = set()

        for ev in events:
            ticker = ev["ticker"]
            event_date = ev["date"]
            mom_pct = float(ev.get("mom_pct", float("nan")))

            try:
                dt_event = datetime.date.fromisoformat(event_date)
            except ValueError:
                continue

            for offset in range(-self.window_days, self.window_days + 1):
                dt_day = dt_event + datetime.timedelta(days=offset)
                day_str = dt_day.isoformat()

                key = (ticker, day_str)
                if key in seen:
                    continue
                seen.add(key)

                if _find_event_dir(ticker, day_str) is None:
                    continue

                # mom_pct labeling: use catalog value on event day, infer on window days.
                if offset == 0:
                    day_mom = mom_pct
                    is_event = True
                else:
                    day_mom = _infer_mom_pct(ticker, day_str)
                    is_event = not (day_mom != day_mom)  # True if not NaN

                yield SessionSpec(
                    tier="tier1",
                    ticker=ticker,
                    date=day_str,
                    mom_pct=day_mom,
                    is_event_day=is_event,
                    scanner_quartile=None,
                    recorded_polls=None,
                )


# ── Tier 0: live ground-truth feed ─────────────────────────────────────

class Tier0Feed(UniverseFeed):
    """Live ground-truth feed from scanner_snapshots.

    Reads recorded polls from either:
      - A PostgreSQL DB (via db_url, requires psycopg2)
      - A pre-exported JSON file (via snapshots_json_path)

    Only yields sessions where tick data exists in FILTERED_DIR.
    Includes ALL tickers in scanner_snapshots (faders included — the scanner
    already filtered to ≥30% before storing).
    """

    def __init__(
        self,
        db_url: Optional[str] = None,
        snapshots_json_path: Optional[Path] = None,
        session_dates: Optional[list[str]] = None,
    ):
        if db_url is None and snapshots_json_path is None:
            raise ValueError("Tier0Feed requires either db_url or snapshots_json_path")
        self.db_url = db_url
        self.snapshots_json_path = snapshots_json_path
        self.session_dates = set(session_dates) if session_dates else None

    def _load_rows_from_db(self) -> list[dict]:
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError:
            raise RuntimeError(
                "psycopg2 not installed. Use --snapshots-json instead.\n"
                "  pip install psycopg2-binary"
            )
        conn = psycopg2.connect(self.db_url)
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if self.session_dates:
                    placeholders = ", ".join(["%s"] * len(self.session_dates))
                    cur.execute(
                        f"SELECT snapshot_ns, session_date::text, snapshot_json "
                        f"FROM scanner_snapshots "
                        f"WHERE session_date IN ({placeholders}) "
                        f"ORDER BY session_date, snapshot_ns",
                        list(self.session_dates),
                    )
                else:
                    cur.execute(
                        "SELECT snapshot_ns, session_date::text, snapshot_json "
                        "FROM scanner_snapshots "
                        "ORDER BY session_date, snapshot_ns"
                    )
                return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def _load_rows_from_file(self) -> list[dict]:
        with open(self.snapshots_json_path) as f:
            return json.load(f)

    def iter_sessions(self) -> Iterator[SessionSpec]:
        rows = self._load_rows_from_db() if self.db_url else self._load_rows_from_file()
        log.info("Tier0Feed: %d snapshot rows loaded", len(rows))

        # Group all polls by (ticker, session_date).
        # polls: list of (snapshot_ns, pct_change_fraction)
        polls_by_key: dict[tuple[str, str], list] = defaultdict(list)
        first_quartile: dict[tuple[str, str], Optional[int]] = {}

        for row in rows:
            snap_ns = int(row["snapshot_ns"])
            date_str = str(row["session_date"])

            if self.session_dates and date_str not in self.session_dates:
                continue

            try:
                items = json.loads(row["snapshot_json"]) if row["snapshot_json"] else []
            except (json.JSONDecodeError, TypeError):
                continue

            for item in items:
                ticker = item.get("ticker")
                if not ticker:
                    continue

                # snapshot_json stores pct_change as percentage (e.g., 35.2 for 35.2%).
                pct_frac = float(item.get("pct_change", 0.0)) / 100.0
                if pct_frac < 0.30:
                    continue  # defensive: should already be filtered

                key = (ticker, date_str)
                polls_by_key[key].append((snap_ns, pct_frac))

                if key not in first_quartile:
                    first_quartile[key] = item.get("scanner_quartile")

        log.info("Tier0Feed: %d (ticker, date) sessions from snapshots", len(polls_by_key))

        for (ticker, date_str), polls in polls_by_key.items():
            polls_sorted = sorted(polls, key=lambda x: x[0])

            if _find_event_dir(ticker, date_str) is None:
                log.debug("Tier0Feed: no parquet for %s %s — skipping", ticker, date_str)
                continue

            mom_pct = _infer_mom_pct(ticker, date_str)
            quartile = first_quartile.get((ticker, date_str))

            yield SessionSpec(
                tier="tier0",
                ticker=ticker,
                date=date_str,
                mom_pct=mom_pct,
                is_event_day=not (mom_pct != mom_pct),  # True if mom_pct is not NaN
                scanner_quartile=quartile,
                recorded_polls=polls_sorted,
            )
