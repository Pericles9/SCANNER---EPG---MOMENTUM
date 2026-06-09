from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

BAND_BY_TIER = {"tier1": 0.05, "tier2": 0.10}


@dataclass
class HaltWindow:
    start: pd.Timestamp
    end: pd.Timestamp
    reason: str = "luld"

    def duration_seconds(self) -> float:
        return float((self.end - self.start).total_seconds())


def _normalize_schedule_tz(schedule: Optional[pd.DataFrame], tz) -> Optional[pd.DataFrame]:
    if schedule is None:
        return None
    # Always return naive timestamps so we can safely compare against tz-naive trade indices.
    if tz is not None:
        schedule = schedule.tz_convert(None)
    else:
        schedule = schedule.tz_localize(None)
    return schedule


def get_trading_schedule(index: pd.Index, calendar_name: str = "NYSE") -> pd.DataFrame:
    try:
        import pandas_market_calendars as mcal
    except Exception as exc:  # pragma: no cover - optional dep
        raise RuntimeError("pandas_market_calendars is required for calendar-aware timelines") from exc

    idx = pd.DatetimeIndex(index).sort_values()
    cal = mcal.get_calendar(calendar_name)
    schedule = cal.schedule(start_date=idx.min().date(), end_date=idx.max().date())
    schedule = _normalize_schedule_tz(schedule, idx.tz)
    return schedule


def build_sessions_from_schedule(schedule: Optional[pd.DataFrame]) -> List[Tuple[pd.Timestamp, pd.Timestamp]]:
    if schedule is None or schedule.empty:
        return []
    sessions: List[Tuple[pd.Timestamp, pd.Timestamp]] = []
    for _, row in schedule.iterrows():
        mo = _to_naive(pd.Timestamp(row["market_open"]))
        mc = _to_naive(pd.Timestamp(row["market_close"]))
        sessions.append((mo, mc))
    return sessions


def _band_pct_for_timestamp(ts: pd.Timestamp, schedule: Optional[pd.DataFrame], base_pct: float) -> float:
    if schedule is None or ts.date() not in schedule.index:
        return base_pct
    session = schedule.loc[ts.date()]
    open_ts = pd.Timestamp(session["market_open"]).to_pydatetime()
    close_ts = pd.Timestamp(session["market_close"]).to_pydatetime()
    if ts < open_ts or ts > close_ts:
        return base_pct
    early_window = pd.Timedelta(minutes=15)
    late_window = pd.Timedelta(minutes=15)
    if ts <= open_ts + early_window or ts >= close_ts - late_window:
        return base_pct * 2.0
    return base_pct


def _choose_columns(df: pd.DataFrame, price_col: Optional[str], size_col: Optional[str]) -> Tuple[str, Optional[str]]:
    if price_col is None:
        price_col = next((c for c in df.columns if "price" in c.lower()), df.columns[0])
    if size_col is None:
        size_col = next((c for c in df.columns if any(x in c.lower() for x in ["size", "volume", "qty"])), None)
    return price_col, size_col


def detect_luld_halts(
    trades: pd.DataFrame,
    price_col: Optional[str] = None,
    size_col: Optional[str] = None,
    schedule: Optional[pd.DataFrame] = None,
    band_tier: str = "tier1",
    base_band_pct: Optional[float] = None,
    limit_state_seconds: int = 15,
    halt_gap_seconds: int = 300,
) -> List[HaltWindow]:
    """Detect LULD-style halts from trade data using 30s VWAP bands and 5m gaps."""
    if trades.empty:
        return []
    price_col, size_col = _choose_columns(trades, price_col, size_col)
    df = trades.sort_index()
    base_pct = base_band_pct if base_band_pct is not None else BAND_BY_TIER.get(band_tier.lower(), 0.05)

    size_series = df[size_col] if size_col else pd.Series(1.0, index=df.index)
    value = df[price_col] * size_series
    ref_price = (value.rolling("30s", min_periods=1).sum() / size_series.rolling("30s", min_periods=1).sum()).ffill()

    band_pct_series = pd.Series([_band_pct_for_timestamp(ts, schedule, base_pct) for ts in df.index], index=df.index)
    upper = ref_price * (1.0 + band_pct_series)
    lower = ref_price * (1.0 - band_pct_series)
    limit_mask = (df[price_col] >= upper) | (df[price_col] <= lower)

    halts: List[HaltWindow] = []
    idx = df.index.to_numpy()
    mask = limit_mask.to_numpy()
    start_idx: Optional[int] = None

    for i, in_band in enumerate(mask):
        if in_band and start_idx is None:
            start_idx = i
        if not in_band and start_idx is not None:
            end_idx = i - 1
            seg_start = pd.Timestamp(idx[start_idx])
            seg_end = pd.Timestamp(idx[end_idx])
            if (seg_end - seg_start).total_seconds() >= limit_state_seconds:
                next_ts = pd.Timestamp(idx[end_idx + 1]) if end_idx + 1 < len(idx) else None
                gap = (next_ts - seg_end).total_seconds() if next_ts is not None else None
                session_row = schedule.loc[seg_end.date()] if schedule is not None and seg_end.date() in schedule.index else None
                session_close = pd.Timestamp(session_row["market_close"]).to_pydatetime() if session_row is not None else None
                session_remaining = (session_close - seg_end).total_seconds() if session_close else None
                qualifies = gap is not None and gap >= halt_gap_seconds
                if qualifies and (session_remaining is None or session_remaining >= halt_gap_seconds):
                    halt_end = next_ts if gap is not None else seg_end + pd.Timedelta(seconds=halt_gap_seconds)
                    if session_close is not None:
                        halt_end = min(halt_end, session_close)
                    halts.append(HaltWindow(start=seg_end, end=pd.Timestamp(halt_end), reason="luld"))
            start_idx = None
    if start_idx is not None:
        seg_start = pd.Timestamp(idx[start_idx])
        seg_end = pd.Timestamp(idx[-1])
        if (seg_end - seg_start).total_seconds() >= limit_state_seconds:
            session_row = schedule.loc[seg_end.date()] if schedule is not None and seg_end.date() in schedule.index else None
            session_close = pd.Timestamp(session_row["market_close"]).to_pydatetime() if session_row is not None else None
            if session_close is not None and (session_close - seg_end).total_seconds() >= halt_gap_seconds:
                halts.append(HaltWindow(start=seg_end, end=pd.Timestamp(session_close), reason="luld"))

    merged: List[HaltWindow] = []
    for h in sorted(halts, key=lambda x: x.start):
        if not merged:
            merged.append(h)
            continue
        last = merged[-1]
        if h.start <= last.end:
            merged[-1] = HaltWindow(start=last.start, end=max(last.end, h.end), reason=last.reason)
        else:
            merged.append(h)
    return merged


def subtract_halts_from_sessions(
    sessions: Sequence[Tuple[pd.Timestamp, pd.Timestamp]], halts: Sequence[HaltWindow]
) -> List[Tuple[pd.Timestamp, pd.Timestamp]]:
    if not sessions:
        return []
    active: List[Tuple[pd.Timestamp, pd.Timestamp]] = []
    halts_naive = [HaltWindow(start=_to_naive(h.start), end=_to_naive(h.end), reason=h.reason) for h in halts]
    halts_sorted = sorted(halts_naive, key=lambda h: h.start)
    for session_start, session_end in sessions:
        session_start = _to_naive(session_start)
        session_end = _to_naive(session_end)
        cursor = session_start
        for h in halts_sorted:
            if h.end <= cursor or h.start >= session_end:
                continue
            if h.start > cursor:
                active.append((cursor, h.start))
            cursor = max(cursor, h.end)
        if cursor < session_end:
            active.append((cursor, session_end))
    return active


def _to_naive(ts: pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is not None:
        return ts.tz_convert(None)
    return ts


def filter_index_to_intervals(index: pd.Index, intervals: Sequence[Tuple[pd.Timestamp, pd.Timestamp]]) -> np.ndarray:
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_convert(None)
    intervals_naive = [(_to_naive(s), _to_naive(e)) for s, e in intervals]
    mask = np.zeros(len(idx), dtype=bool)
    intervals_sorted = sorted(intervals_naive, key=lambda x: x[0])
    interval_ptr = 0
    for i, ts in enumerate(idx):
        while interval_ptr < len(intervals_sorted) and ts > intervals_sorted[interval_ptr][1]:
            interval_ptr += 1
        if interval_ptr >= len(intervals_sorted):
            break
        start, end = intervals_sorted[interval_ptr]
        if start <= ts <= end:
            mask[i] = True
    return mask


def compress_active_seconds(index: pd.Index, active_intervals: Sequence[Tuple[pd.Timestamp, pd.Timestamp]]) -> np.ndarray:
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_convert(None)
    if len(idx) == 0:
        return np.array([], dtype=float)
    intervals = sorted([(_to_naive(s), _to_naive(e)) for s, e in active_intervals], key=lambda x: x[0])
    active_seconds = np.zeros(len(idx), dtype=float)
    interval_ptr = 0
    elapsed = 0.0
    for i, ts in enumerate(idx):
        while interval_ptr < len(intervals) and ts > intervals[interval_ptr][1]:
            elapsed += (intervals[interval_ptr][1] - intervals[interval_ptr][0]).total_seconds()
            interval_ptr += 1
        if interval_ptr >= len(intervals):
            active_seconds[i] = elapsed
            continue
        start, end = intervals[interval_ptr]
        if ts < start:
            active_seconds[i] = elapsed
        else:
            active_seconds[i] = elapsed + (ts - start).total_seconds()
    active_seconds -= active_seconds[0]
    return active_seconds


def prepare_active_trades(
    trades: pd.DataFrame,
    price_col: Optional[str] = None,
    size_col: Optional[str] = None,
    calendar_name: str = "NYSE",
    band_tier: str = "tier1",
    base_band_pct: Optional[float] = None,
    limit_state_seconds: int = 15,
    halt_gap_seconds: int = 300,
    include_extended: bool = False,
) -> Tuple[pd.DataFrame, np.ndarray, dict]:
    if trades.empty:
        return trades, np.array([], dtype=float), {"halts": [], "sessions": [], "active_intervals": []}

    if isinstance(trades.index, pd.DatetimeIndex) and trades.index.tz is not None:
        trades = trades.copy()
        trades.index = trades.index.tz_convert(None)

    schedule = get_trading_schedule(trades.index, calendar_name=calendar_name)
    
    if include_extended:
        # Extended hours: 04:00 to 20:00 (8 PM)
        sessions = []
        unique_dates = np.unique(trades.index.date)
        for d in unique_dates:
            start = pd.Timestamp(year=d.year, month=d.month, day=d.day, hour=4)
            end = pd.Timestamp(year=d.year, month=d.month, day=d.day, hour=20)
            sessions.append((start, end))
    else:
        sessions = build_sessions_from_schedule(schedule)

    halts = detect_luld_halts(
        trades,
        price_col=price_col,
        size_col=size_col,
        schedule=schedule,
        band_tier=band_tier,
        base_band_pct=base_band_pct,
        limit_state_seconds=limit_state_seconds,
        halt_gap_seconds=halt_gap_seconds,
    )
    active_intervals = subtract_halts_from_sessions(sessions, halts)

    mask = filter_index_to_intervals(trades.index, active_intervals)
    trimmed = trades.loc[mask].copy()
    active_seconds = compress_active_seconds(trimmed.index, active_intervals) if len(trimmed) else np.array([], dtype=float)

    meta = {"halts": halts, "sessions": sessions, "active_intervals": active_intervals, "schedule": schedule}
    return trimmed, active_seconds, meta


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Detect LULD halts and build active timeline")
    parser.add_argument("--trades", type=Path, required=True, help="Path to trades parquet file")
    parser.add_argument("--price-col", dest="price_col", default=None, help="Price column name")
    parser.add_argument("--size-col", dest="size_col", default=None, help="Size/volume column name")
    parser.add_argument("--band-tier", dest="band_tier", default="tier1", choices=["tier1", "tier2"], help="Band tier (5% or 10%)")
    parser.add_argument("--band-pct", dest="band_pct", type=float, default=None, help="Override band pct (e.g., 0.05)")
    parser.add_argument("--calendar", dest="calendar", default="NYSE", help="Exchange calendar name")
    parser.add_argument("--limit-state-seconds", dest="limit_state_seconds", type=int, default=15, help="Seconds price must stay at band before halt timer")
    parser.add_argument("--halt-gap-seconds", dest="halt_gap_seconds", type=int, default=300, help="Gap threshold to confirm halt")
    args = parser.parse_args()

    df = pd.read_parquet(args.trades)
    ts_col = next((c for c in df.columns if any(x in c.lower() for x in ["time", "ts"])), None)
    if ts_col is None:
        raise ValueError("No timestamp column found (expected something like time or ts)")
    df[ts_col] = pd.to_datetime(df[ts_col])
    df = df.sort_values(ts_col).set_index(ts_col)

    trimmed, active_seconds, meta = prepare_active_trades(
        df,
        price_col=args.price_col,
        size_col=args.size_col,
        calendar_name=args.calendar,
        band_tier=args.band_tier,
        base_band_pct=args.band_pct,
        limit_state_seconds=args.limit_state_seconds,
        halt_gap_seconds=args.halt_gap_seconds,
    )

    total_removed = len(df) - len(trimmed)
    halt_seconds = sum(h.duration_seconds() for h in meta.get("halts", []))
    print(f"Trades kept: {len(trimmed)} / {len(df)} (removed {total_removed})")
    print(f"Detected halts: {len(meta.get('halts', []))}; removed {halt_seconds/60:.2f} minutes of halted time")
    for h in meta.get("halts", []):
        print(f"- {h.reason} halt {h.start} -> {h.end} ({h.duration_seconds()/60:.2f} min)")
    if len(active_seconds):
        print(f"Active duration: {active_seconds[-1]:.2f} seconds from first to last trade")


if __name__ == "__main__":
    _cli()
