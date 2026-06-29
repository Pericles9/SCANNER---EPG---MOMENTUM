#!/usr/bin/env python3
"""
Phase DIAG-TAPE — Pre-Event Price Action Charts.

T1: Load full trades.parquet (no date slicing), characterise T-1 data availability,
    classify where the price gap occurs, write data_availability.json.
T2: Generate 3-panel Plotly HTML chart per event (42 total).
T3: Write sortable charts/index.html.
T4: Write summary.md.

Run: python -m backtest.scripts.diag_tape   (from repo root)
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal
import plotly.graph_objects as go
import pyarrow.parquet as pq
import pytz
from plotly.subplots import make_subplots

BACKTEST = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKTEST))
sys.path.insert(0, str(BACKTEST.parent))

from data.schemas.mom_db import FILTERED_DIR, NS_PER_SECOND  # noqa: E402
from data.loaders.prev_close import get_prev_close  # noqa: E402

OUT = BACKTEST / "results" / "phase_diag_tape"
CHARTS_DIR = OUT / "charts"
OUT.mkdir(parents=True, exist_ok=True)
CHARTS_DIR.mkdir(parents=True, exist_ok=True)

AUDIT_PATH = BACKTEST / "results" / "phase_diag_entry" / "entry_audit.json"

ET = pytz.timezone("America/New_York")
NYSE = mcal.get_calendar("NYSE")

# 5 diverse traded controls (ticker, date)
CONTROL_KEYS = {
    ("VS", "2023-11-24"),
    ("RBOT", "2023-12-20"),
    ("MNY", "2024-02-20"),
    ("PALI", "2024-04-16"),
    ("KOSS", "2024-07-03"),
}

FAILURE_COLORS = {
    "TRADED": "green",
    "WARMUP_AT_DEADLINE": "orange",
    "ANCHOR_LATE": "red",
    "NEVER_PASS_IN_WINDOW": "darkred",
    "PASS_TOO_LATE": "orange",
}

SESSION_SHADE = {
    "t1_pm":     dict(fillcolor="rgba(173,216,230,0.35)", line_width=0, layer="below"),
    "overnight": dict(fillcolor="rgba(50,50,50,0.40)",    line_width=0, layer="below"),
    "t_pre":     dict(fillcolor="rgba(255,255,200,0.40)", line_width=0, layer="below"),
    "t_pm":      dict(fillcolor="rgba(173,216,230,0.35)", line_width=0, layer="below"),
}


# ── Utilities ────────────────────────────────────────────────────────────────

def prior_trading_day(date_str: str) -> Optional[str]:
    date = pd.Timestamp(date_str)
    start = date - pd.Timedelta(days=12)
    schedule = NYSE.schedule(
        start_date=start.strftime("%Y-%m-%d"),
        end_date=(date - pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
    )
    if schedule.empty:
        return None
    return schedule.index[-1].strftime("%Y-%m-%d")


def find_event_path(ticker: str, date: str) -> Optional[Path]:
    candidates = list(FILTERED_DIR.glob(f"{ticker}_{date}_*"))
    if not candidates:
        return None
    return sorted(candidates)[-1]


def load_full_trades(event_path: Path) -> Optional[pd.DataFrame]:
    trades_path = event_path / "trades.parquet"
    if not trades_path.exists():
        return None
    table = pq.read_table(str(trades_path), columns=["sip_timestamp", "price", "size"])
    df = pd.DataFrame({
        "ts_ns": table.column("sip_timestamp").to_numpy().astype(np.int64),
        "price":  table.column("price").to_numpy().astype(np.float64),
        "size":   table.column("size").to_numpy().astype(np.int64),
    })
    df.sort_values("ts_ns", inplace=True)
    df.reset_index(drop=True, inplace=True)
    df["dt_et"] = pd.to_datetime(df["ts_ns"], unit="ns", utc=True).dt.tz_convert(ET)
    df["date_et"] = df["dt_et"].dt.date.astype(str)
    return df


def _date_hour_minute(dt_series: pd.Series):
    h = dt_series.dt.hour
    m = dt_series.dt.minute
    return h, m


def make_masks(df: pd.DataFrame, t1_date: str, event_date: str):
    d = df["date_et"]
    h = df["dt_et"].dt.hour
    m = df["dt_et"].dt.minute

    t1_rth   = (d == t1_date) & ((h == 9) & (m >= 30) | (h >= 10) & (h < 16))
    t1_pm    = (d == t1_date) & (h >= 16) & (h < 20)
    t_pre    = (d == event_date) & (h >= 4) & ((h < 9) | ((h == 9) & (m < 30)))
    t_rth    = (d == event_date) & ((h == 9) & (m >= 30) | (h >= 10) & (h < 16))
    t_pm     = (d == event_date) & (h >= 16) & (h < 20)
    t_session = (d == event_date) & (h >= 4) & (h < 20)  # 4am–8pm

    return dict(t1_rth=t1_rth, t1_pm=t1_pm, t_pre=t_pre,
                t_rth=t_rth, t_pm=t_pm, t_session=t_session)


def classify_gap(
    df: pd.DataFrame,
    masks: dict,
    price_at_t1_close: Optional[float],
    price_at_first_t_trade: Optional[float],
) -> str:
    n_t1_pm  = int(masks["t1_pm"].sum())
    n_t_pre  = int(masks["t_pre"].sum())

    # T1_POSTMARKET: T-1 PM has trades AND price rose >10% vs T-1 regular close
    if n_t1_pm > 0 and price_at_t1_close and price_at_t1_close > 0:
        t1_pm_end = float(df.loc[masks["t1_pm"], "price"].iloc[-1])
        if (t1_pm_end / price_at_t1_close - 1) > 0.10:
            return "T1_POSTMARKET"

    # T_PREMARKET: price rises gradually from near prev during 04:00–09:30 T
    if n_t_pre >= 5 and price_at_t1_close and price_at_t1_close > 0:
        t_pre_prices = df.loc[masks["t_pre"], "price"]
        first_pre = float(t_pre_prices.iloc[0])
        last_pre  = float(t_pre_prices.iloc[-1])
        if first_pre < price_at_t1_close * 1.15 and last_pre >= price_at_t1_close * 1.30:
            return "T_PREMARKET"

    # OVERNIGHT_NO_TAPE: first T trade already gapped
    if (price_at_first_t_trade is not None and price_at_t1_close is not None
            and price_at_t1_close > 0):
        if (price_at_first_t_trade / price_at_t1_close - 1) > 0.10:
            return "OVERNIGHT_NO_TAPE"

    return "UNKNOWN"


# ── T1: per-event analysis ────────────────────────────────────────────────────

def analyze_event(audit_row: dict, is_control: bool) -> dict:
    ticker = audit_row["ticker"]
    date   = audit_row["date"]

    event_path = find_event_path(ticker, date)
    if event_path is None:
        return {"ticker": ticker, "date": date, "is_control": is_control,
                "error": "no_event_dir", "gap_occurs_in": "UNKNOWN"}

    df = load_full_trades(event_path)
    if df is None or len(df) == 0:
        return {"ticker": ticker, "date": date, "is_control": is_control,
                "error": "no_trades", "gap_occurs_in": "UNKNOWN"}

    t1_date = prior_trading_day(date)
    if t1_date is None:
        return {"ticker": ticker, "date": date, "is_control": is_control,
                "error": "no_t1_date", "gap_occurs_in": "UNKNOWN"}

    masks = make_masks(df, t1_date, date)

    # Counts
    n_t1_pm  = int(masks["t1_pm"].sum())
    n_t_pre  = int(masks["t_pre"].sum())
    n_t_rth  = int(masks["t_rth"].sum())

    # Earliest / latest timestamps in full parquet
    earliest_ts_et = df["dt_et"].iloc[0].isoformat()
    latest_ts_et   = df["dt_et"].iloc[-1].isoformat()

    # T-1 regular close: last trade before 16:00 on T-1
    t1_rth_df = df[masks["t1_rth"]]
    price_at_t1_close = float(t1_rth_df["price"].iloc[-1]) if len(t1_rth_df) > 0 else None

    # Event-day session first trade (4am–8pm on event date)
    t_session_df = df[masks["t_session"]]
    if len(t_session_df) == 0:
        # Fallback: any trade on event date
        t_session_df = df[df["date_et"] == date]

    price_at_first_t_trade  = float(t_session_df["price"].iloc[0]) if len(t_session_df) > 0 else None
    first_trade_wall_clock_et = t_session_df["dt_et"].iloc[0].isoformat() if len(t_session_df) > 0 else None

    # Gap from T-1 close
    gap_from_t1_close = None
    if price_at_t1_close and price_at_first_t_trade and price_at_t1_close > 0:
        gap_from_t1_close = (price_at_first_t_trade / price_at_t1_close - 1) * 100

    # Gap classification
    gap_occurs_in = classify_gap(df, masks, price_at_t1_close, price_at_first_t_trade)

    # prev_close from runner's source (for chart annotation)
    try:
        prev_close = get_prev_close(ticker, date)
    except Exception:
        prev_close = None

    # Wall-clock anchor and scanner hit ET times
    # t_event_anchor_sec is from td.timestamps[0] (first session trade on event day)
    scanner_hit_wall_clock_et     = None
    t_event_anchor_wall_clock_et  = None
    if first_trade_wall_clock_et is not None:
        ref_et = t_session_df["dt_et"].iloc[0]
        scanner_hit_offset = timedelta(seconds=float(audit_row.get("scanner_hit_t_sec", 0) or 0))
        scanner_hit_et = ref_et + scanner_hit_offset
        scanner_hit_wall_clock_et = scanner_hit_et.isoformat()

        anchor_sec = audit_row.get("t_event_anchor_sec")
        if anchor_sec is not None:
            anchor_et = ref_et + timedelta(seconds=float(anchor_sec))
            t_event_anchor_wall_clock_et = anchor_et.isoformat()

    return {
        "ticker": ticker,
        "date":   date,
        "is_control": is_control,
        "stratum": audit_row.get("stratum"),
        "gap_pct_at_hit": audit_row.get("gap_pct_at_hit"),
        "t_day_minus1_date": t1_date,
        "earliest_ts_et": earliest_ts_et,
        "latest_ts_et":   latest_ts_et,
        "n_trades_t1_postmarket": n_t1_pm,
        "n_trades_t_premarket":   n_t_pre,
        "n_trades_t_regular":     n_t_rth,
        "t1_postmarket_present":  n_t1_pm > 0,
        "first_trade_wall_clock_et": first_trade_wall_clock_et,
        "price_at_t1_close":       price_at_t1_close,
        "price_at_first_t_trade":  price_at_first_t_trade,
        "gap_from_t1_close":       gap_from_t1_close,
        "gap_occurs_in":           gap_occurs_in,
        "prev_close": prev_close,
        "scanner_hit_wall_clock_et":    scanner_hit_wall_clock_et,
        "t_event_anchor_wall_clock_et": t_event_anchor_wall_clock_et,
        # Pass-through from entry_audit
        "n_trades_before_scanner":    audit_row.get("n_trades_before_scanner"),
        "lambda_ref_cold_start":      audit_row.get("lambda_ref_cold_start"),
        "anchor_fired":               audit_row.get("anchor_fired"),
        "t_event_anchor_relative_sec": audit_row.get("t_event_anchor_relative_sec"),
        "t_event_anchor_sec":         audit_row.get("t_event_anchor_sec"),
        "gate_state_at_scanner_hit":  audit_row.get("gate_state_at_scanner_hit"),
        "any_pass_in_entry_window":   audit_row.get("any_pass_in_entry_window"),
        "entry_failure_reason":       audit_row.get("entry_failure_reason"),
        "t_first_pass_relative_sec":  audit_row.get("t_first_pass_relative_sec"),
        "scanner_hit_t_sec":          audit_row.get("scanner_hit_t_sec"),
    }


# ── T2: chart generation ──────────────────────────────────────────────────────

def _make_ohlc_vol(df: pd.DataFrame, chart_start: pd.Timestamp,
                   chart_end: pd.Timestamp, t1_date: str, event_date: str):
    """Aggregate to 1-min OHLC + dollar volume within the chart window.

    Process each trading session independently so carry-forward never crosses
    the overnight gap. Overnight region is simply absent (no bars).
    """
    df_w = df.copy()
    df_w["dv"] = df_w["price"] * df_w["size"]
    df_w = df_w.set_index("dt_et")

    # Two trading session intervals (skip overnight 20:00 T-1 → 04:00 T)
    session_intervals = [
        (chart_start,                                            pd.Timestamp(f"{t1_date} 20:00:00", tz=ET)),
        (pd.Timestamp(f"{event_date} 04:00:00", tz=ET),         chart_end),
    ]

    ohlc_parts = []
    vol_parts  = []

    for s, e in session_intervals:
        # Minute grid for this session (e is exclusive: don't include the endpoint bar)
        min_range = pd.date_range(s, e - pd.Timedelta(minutes=1), freq="1min", tz=ET)
        if len(min_range) == 0:
            continue

        # Raw aggregation within session window
        seg_data = df_w[(df_w.index >= s) & (df_w.index < e)]
        if len(seg_data) > 0:
            raw_ohlc = seg_data["price"].resample("1min").agg(
                open="first", high="max", low="min", close="last"
            )
            raw_vol = seg_data["dv"].resample("1min").sum()
        else:
            raw_ohlc = pd.DataFrame(
                {"open": np.nan, "high": np.nan, "low": np.nan, "close": np.nan},
                index=pd.DatetimeIndex([], tz=ET),
            )
            raw_vol = pd.Series(dtype=float, index=pd.DatetimeIndex([], tz=ET))

        # Reindex to full minute grid
        ohlc_s = raw_ohlc.reindex(min_range)
        vol_s  = raw_vol.reindex(min_range, fill_value=0.0)

        # Carry-forward: for NaN bars, make flat bar at prior close
        # Use .where on the segment only — no cross-session index mixing
        was_nan = ohlc_s["open"].isna().values          # numpy bool array
        close_ffilled = ohlc_s["close"].ffill().values  # numpy float array

        for col in ["open", "high", "low", "close"]:
            vals = ohlc_s[col].values.copy()
            # Fill NaN positions with prior close (if available)
            vals[was_nan] = close_ffilled[was_nan]
            ohlc_s[col] = vals

        ohlc_parts.append(ohlc_s)
        vol_parts.append(vol_s)

    if not ohlc_parts:
        return pd.DataFrame(), pd.Series(dtype=float)

    ohlc = pd.concat(ohlc_parts)
    vol  = pd.concat(vol_parts)

    ohlc = ohlc.dropna(subset=["open"])
    vol  = vol.reindex(ohlc.index, fill_value=0.0)
    return ohlc, vol


def _add_session_shading(fig: go.Figure, t1_date: str, event_date: str,
                          n_rows: int):
    """Add vrect shading for all session regions, to every subplot row."""
    regions = [
        (f"{t1_date} 16:00", f"{t1_date} 20:00",  "t1_pm",    "T-1 Post-Market"),
        (f"{t1_date} 20:00", f"{event_date} 04:00", "overnight", "Overnight (no tape)"),
        (f"{event_date} 04:00", f"{event_date} 09:30", "t_pre", "T Pre-Market"),
        (f"{event_date} 16:00", f"{event_date} 20:00", "t_pm",  "T Post-Market"),
    ]
    for row in range(1, n_rows + 1):
        for x0s, x1s, key, label in regions:
            x0 = pd.Timestamp(x0s, tz=ET)
            x1 = pd.Timestamp(x1s, tz=ET)
            kw = SESSION_SHADE[key]
            fig.add_vrect(
                x0=x0, x1=x1,
                fillcolor=kw["fillcolor"], line_width=kw["line_width"],
                layer=kw["layer"],
                annotation_text=label if row == 1 else "",
                annotation_position="top left",
                annotation_font_size=9,
                row=row, col=1,
            )


def _vline(fig, x, color, dash, label, row=1):
    fig.add_vline(x=x.timestamp() * 1000, line_color=color, line_dash=dash,
                  line_width=1.2, row=row, col=1)
    if row == 1 and label:
        fig.add_annotation(
            x=x, y=1.01, xref="x", yref="paper",
            text=label, showarrow=False,
            font=dict(size=8, color=color),
            textangle=-45,
            xanchor="left",
        )


def generate_chart(ev: dict, df: pd.DataFrame) -> Optional[go.Figure]:
    ticker = ev["ticker"]
    date   = ev["date"]
    t1_date = ev["t_day_minus1_date"]
    is_control = ev["is_control"]

    chart_start = pd.Timestamp(f"{t1_date} 14:00:00", tz=ET)
    chart_end   = pd.Timestamp(f"{date} 20:00:00",    tz=ET)

    ohlc, vol = _make_ohlc_vol(df, chart_start, chart_end, t1_date, date)
    if len(ohlc) == 0:
        return None

    # Colours for candlestick
    inc_color = "rgba(0,180,0,0.85)"
    dec_color = "rgba(220,0,0,0.85)"

    # ── Subplots ────────────────────────────────────────────────────────
    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        row_heights=[0.50, 0.30, 0.20],
        vertical_spacing=0.04,
        subplot_titles=["Price (1-min OHLC)", "Dollar Volume / Min", "Entry Signal Summary"],
    )

    # ── Panel 1 — Candlestick ────────────────────────────────────────────
    fig.add_trace(go.Candlestick(
        x=ohlc.index,
        open=ohlc["open"], high=ohlc["high"],
        low=ohlc["low"],   close=ohlc["close"],
        increasing_line_color=inc_color, decreasing_line_color=dec_color,
        increasing_fillcolor=inc_color,  decreasing_fillcolor=dec_color,
        line_width=1,
        showlegend=False,
        name="Price",
    ), row=1, col=1)

    # Prev close reference lines
    prev_close = ev.get("prev_close")
    if prev_close and prev_close > 0:
        threshold_30 = prev_close * 1.30
        fig.add_hline(y=prev_close,    line_dash="dash", line_color="gray",
                      line_width=1.2, annotation_text=f"Prev Close {prev_close:.2f}",
                      annotation_position="left", row=1, col=1)
        fig.add_hline(y=threshold_30,  line_dash="dash", line_color="blue",
                      line_width=1.2, annotation_text=f"30% threshold ({threshold_30:.2f})",
                      annotation_position="left", row=1, col=1)

    # Session shading
    _add_session_shading(fig, t1_date, date, n_rows=3)

    # Session boundary vertical lines (thin gray) – all rows
    for ts_str in [f"{t1_date} 16:00", f"{date} 04:00", f"{date} 09:30", f"{date} 16:00"]:
        ts = pd.Timestamp(ts_str, tz=ET)
        for row in range(1, 4):
            fig.add_vline(x=ts.timestamp() * 1000, line_color="gray",
                          line_dash="dot", line_width=0.8, row=row, col=1)

    # Scanner hit line
    scanner_hit_str = ev.get("scanner_hit_wall_clock_et")
    if scanner_hit_str:
        try:
            scanner_hit_ts = pd.Timestamp(scanner_hit_str)
            for row in range(1, 4):
                fig.add_vline(x=scanner_hit_ts.timestamp() * 1000,
                              line_color="blue", line_dash="dash", line_width=1.5,
                              row=row, col=1)
            fig.add_annotation(
                x=scanner_hit_ts, y=1.02, xref="x", yref="paper",
                text="Scanner Hit", showarrow=False,
                font=dict(size=8, color="blue"), textangle=-45, xanchor="left",
            )
            # Entry deadline (+300s)
            deadline_ts = scanner_hit_ts + pd.Timedelta(seconds=300)
            for row in range(1, 4):
                fig.add_vline(x=deadline_ts.timestamp() * 1000,
                              line_color="orange", line_dash="dash", line_width=1.2,
                              row=row, col=1)
            fig.add_annotation(
                x=deadline_ts, y=1.02, xref="x", yref="paper",
                text="Entry Deadline (+300s)", showarrow=False,
                font=dict(size=8, color="orange"), textangle=-45, xanchor="left",
            )
        except Exception:
            pass

    # EventAnchor line
    anchor_str = ev.get("t_event_anchor_wall_clock_et")
    if anchor_str:
        try:
            anchor_ts = pd.Timestamp(anchor_str)
            # Omit if anchor fired after T 20:00
            if anchor_ts <= chart_end:
                for row in range(1, 4):
                    fig.add_vline(x=anchor_ts.timestamp() * 1000,
                                  line_color="purple", line_dash="dash", line_width=1.2,
                                  row=row, col=1)
                fig.add_annotation(
                    x=anchor_ts, y=1.07, xref="x", yref="paper",
                    text="EventAnchor", showarrow=False,
                    font=dict(size=8, color="purple"), textangle=-45, xanchor="left",
                )
        except Exception:
            pass

    # Entry line for TRADED controls
    if is_control and scanner_hit_str:
        t_fp = ev.get("t_first_pass_relative_sec")
        if t_fp is not None:
            try:
                scanner_hit_ts = pd.Timestamp(scanner_hit_str)
                entry_ts = scanner_hit_ts + pd.Timedelta(seconds=float(t_fp))
                # Find entry price at that time
                entry_idx = (df["dt_et"] - entry_ts).abs().idxmin()
                entry_price = float(df.loc[entry_idx, "price"])
                for row in range(1, 4):
                    fig.add_vline(x=entry_ts.timestamp() * 1000,
                                  line_color="green", line_dash="dash", line_width=1.5,
                                  row=row, col=1)
                fig.add_annotation(
                    x=entry_ts, y=1.12, xref="x", yref="paper",
                    text=f"Entry @ {entry_price:.4f}", showarrow=False,
                    font=dict(size=8, color="green"), textangle=-45, xanchor="left",
                )
            except Exception:
                pass

    # ── Panel 2 — Dollar volume ──────────────────────────────────────────
    vol_values = vol.values.astype(float)
    _pos = vol_values[vol_values > 0]
    use_log = bool(
        len(_pos) > 1 and (_pos.max() - _pos.min()) > 100 * _pos.min()
    )

    fig.add_trace(go.Bar(
        x=vol.index,
        y=vol_values,
        marker_color="gray",
        name="Dollar Vol",
        showlegend=False,
    ), row=2, col=1)

    if use_log:
        fig.update_yaxes(type="log", row=2, col=1)
    fig.update_yaxes(title_text="$ Vol / Min", row=2, col=1, title_font_size=10)

    # ── Panel 3 — Signal summary strip ──────────────────────────────────
    entry_failure = ev.get("entry_failure_reason", "UNKNOWN") or "UNKNOWN"
    fail_color    = FAILURE_COLORS.get(entry_failure, "black")

    n_pre      = ev.get("n_trades_before_scanner", "?")
    lref       = ev.get("lambda_ref_cold_start")
    lref_str   = f"{lref:.4f}" if lref is not None else "N/A"
    anchor_f   = ev.get("anchor_fired", False)
    t_anc_rel  = ev.get("t_event_anchor_relative_sec")
    t_anc_str  = f"{t_anc_rel:.0f}s" if t_anc_rel is not None else "N/A"
    gate_state = ev.get("gate_state_at_scanner_hit", "?")
    any_pass   = ev.get("any_pass_in_entry_window", False)

    if is_control:
        lag = ev.get("t_first_pass_relative_sec")
        lag_str = f"{lag:.1f}s" if lag is not None else "N/A"
        failure_text = f"Failure reason: TRADED | Entry lag: {lag_str}"
    else:
        failure_text = f"Failure reason: {entry_failure}"

    annotations_p3 = [
        (0.85, f"n_trades_before_scanner: {n_pre}"),
        (0.70, f"λ_ref (cold start): {lref_str}"),
        (0.55, f"Anchor: {anchor_f} | t_event_relative: {t_anc_str}"),
        (0.40, f"Gate at scanner hit: {gate_state}"),
        (0.25, f"Any PASS in entry window: {any_pass}"),
        (0.10, failure_text),
    ]

    # Dummy scatter to anchor the y-axis for row 3
    fig.add_trace(go.Scatter(
        x=[chart_start, chart_end],
        y=[0.5, 0.5],
        mode="markers",
        marker=dict(opacity=0),
        showlegend=False,
    ), row=3, col=1)

    for y_pos, text in annotations_p3:
        color = fail_color if "Failure reason" in text else "black"
        fig.add_annotation(
            x=chart_start + (chart_end - chart_start) * 0.01,
            y=y_pos,
            xref="x3",
            yref="y3",
            text=text,
            showarrow=False,
            font=dict(size=10, color=color),
            xanchor="left",
        )

    # Entry window colour strip (y=0 to y=0.05, spanning scanner_hit to deadline)
    if scanner_hit_str:
        try:
            scanner_hit_ts = pd.Timestamp(scanner_hit_str)
            deadline_ts = scanner_hit_ts + pd.Timedelta(seconds=300)
            strip_color = "rgba(0,200,0,0.6)" if any_pass else "rgba(220,0,0,0.6)"
            fig.add_shape(
                type="rect",
                x0=scanner_hit_ts, x1=deadline_ts,
                y0=0.0, y1=0.05,
                xref="x3", yref="y3",
                fillcolor=strip_color, line_width=0,
                layer="below",
            )
        except Exception:
            pass

    # ── Layout ────────────────────────────────────────────────────────────
    gap_label = ev.get("gap_occurs_in", "UNKNOWN")
    first_trade_str = ev.get("first_trade_wall_clock_et", "?")
    n_pre_label = ev.get("n_trades_before_scanner", "?")
    fail_label  = ev.get("entry_failure_reason", "?")

    # Extract HH:MM from ISO string
    first_trade_hhmm = "?"
    if first_trade_str and first_trade_str != "?":
        try:
            ft = pd.Timestamp(first_trade_str)
            first_trade_hhmm = ft.strftime("%H:%M")
        except Exception:
            pass

    title = (
        f"{ticker} {date} | {gap_label} | "
        f"1st trade: {first_trade_hhmm} ET | "
        f"n_pre: {n_pre_label} | {fail_label}"
    )
    if is_control:
        title += " | [CONTROL — TRADED]"

    fig.update_layout(
        title=dict(text=title, font=dict(size=12)),
        height=900, width=1500,
        xaxis_rangeslider_visible=False,
        xaxis3_rangeslider_visible=False,
        legend=dict(orientation="h", x=1, xanchor="right", y=1.05),
        margin=dict(l=80, r=40, t=80, b=40),
        template="plotly_white",
    )

    # Force x-axis range on all panels
    x_range = [chart_start, chart_end]
    fig.update_xaxes(range=x_range)

    # Hide y-axis ticks on panel 3
    fig.update_yaxes(visible=False, row=3, col=1)

    # Label x-axis of bottom panel
    fig.update_xaxes(title_text="Wall-clock time (ET)", row=3, col=1)

    return fig


# ── T3: index.html ─────────────────────────────────────────────────────────────

def write_index(results: list[dict]):
    rows_html = []
    for ev in results:
        if ev.get("error"):
            continue
        ticker    = ev["ticker"]
        date      = ev["date"]
        is_ctrl   = ev["is_control"]
        gap       = ev.get("gap_occurs_in", "UNKNOWN")
        t1_pm     = "Yes" if ev.get("t1_postmarket_present") else "No"
        n_t1_pm   = ev.get("n_trades_t1_postmarket", 0)
        n_pre     = ev.get("n_trades_before_scanner", "?")
        fail      = ev.get("entry_failure_reason", "?")
        first_ts  = ev.get("first_trade_wall_clock_et", "")
        first_hhmm = "?"
        if first_ts:
            try:
                first_hhmm = pd.Timestamp(first_ts).strftime("%H:%M ET")
            except Exception:
                pass

        evt_type = "Control" if is_ctrl else "1st-appear"
        fname = f"{ticker}_{date}_CONTROL.html" if is_ctrl else f"{ticker}_{date}_{gap}.html"
        link = f'<a href="{fname}">{ticker} {date}</a>'

        row_class = 'class="control"' if is_ctrl else ""
        rows_html.append(
            f"<tr {row_class}>"
            f"<td>{evt_type}</td>"
            f"<td>{link}</td>"
            f"<td>{gap}</td>"
            f"<td>{first_hhmm}</td>"
            f"<td>{n_pre}</td>"
            f"<td>{t1_pm}</td>"
            f"<td>{n_t1_pm}</td>"
            f"<td>{fail}</td>"
            f"</tr>"
        )

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Phase DIAG-TAPE — Event Index</title>
<style>
  body {{ font-family: monospace; font-size: 12px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ccc; padding: 4px 8px; }}
  th {{ background: #eee; cursor: pointer; }}
  tr.control {{ background: #e8f4e8; }}
  tr:hover {{ background: #ffffcc; }}
  a {{ color: #0066cc; }}
</style>
<script>
function sortTable(col) {{
  var tbl = document.getElementById("evt");
  var rows = Array.from(tbl.rows);
  var asc = tbl.getAttribute("data-sort-col") == col
            && tbl.getAttribute("data-sort-dir") == "asc";
  rows.sort(function(a, b) {{
    var av = a.cells[col].textContent.trim();
    var bv = b.cells[col].textContent.trim();
    return asc ? bv.localeCompare(av) : av.localeCompare(bv);
  }});
  rows.forEach(function(r) {{ tbl.appendChild(r); }});
  tbl.setAttribute("data-sort-col", col);
  tbl.setAttribute("data-sort-dir", asc ? "desc" : "asc");
}}
</script>
</head>
<body>
<h2>Phase DIAG-TAPE — Pre-Event Price Action Index</h2>
<p>{len([r for r in results if not r.get("error")])} events |
   Generated {pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")}</p>
<table id="evt">
<thead><tr>
  <th onclick="sortTable(0)">Type</th>
  <th onclick="sortTable(1)">Event</th>
  <th onclick="sortTable(2)">Gap occurs in</th>
  <th onclick="sortTable(3)">1st trade ET</th>
  <th onclick="sortTable(4)">n_pre-scanner</th>
  <th onclick="sortTable(5)">T-1 PM present</th>
  <th onclick="sortTable(6)">T-1 PM trades</th>
  <th onclick="sortTable(7)">Entry failure</th>
</tr></thead>
<tbody>
{"".join(rows_html)}
</tbody>
</table>
</body>
</html>"""

    (CHARTS_DIR / "index.html").write_text(html, encoding="utf-8")
    print(f"  Wrote {CHARTS_DIR / 'index.html'}")


# ── T4: summary.md ─────────────────────────────────────────────────────────────

def write_summary(results: list[dict]):
    first_app = [r for r in results if not r.get("is_control") and not r.get("error")]
    controls  = [r for r in results if r.get("is_control") and not r.get("error")]
    errors    = [r for r in results if r.get("error")]

    # T-1 PM availability
    n_t1_pm_present = sum(1 for r in first_app if r.get("t1_postmarket_present"))
    n_t1_pm_absent  = len(first_app) - n_t1_pm_present

    # First trade distribution (first-appearance only)
    first_trades = []
    for r in first_app:
        ft = r.get("first_trade_wall_clock_et")
        if ft:
            try:
                first_trades.append(pd.Timestamp(ft))
            except Exception:
                pass

    # Sort by time-of-day (not by absolute date+time)
    first_trades_sorted = sorted(first_trades, key=lambda t: (t.hour, t.minute, t.second))
    ft_min = first_trades_sorted[0].strftime("%H:%M ET") if first_trades_sorted else "N/A"
    ft_max = first_trades_sorted[-1].strftime("%H:%M ET") if first_trades_sorted else "N/A"
    ft_med = first_trades_sorted[len(first_trades_sorted) // 2].strftime("%H:%M ET") \
        if first_trades_sorted else "N/A"
    ft_before_0600 = sum(1 for t in first_trades if t.hour < 6)
    ft_before_0930 = sum(1 for t in first_trades if t.hour < 9 or (t.hour == 9 and t.minute < 30))

    # Gap location breakdown
    from collections import Counter
    gap_counts = Counter(r.get("gap_occurs_in", "UNKNOWN") for r in first_app)

    # T1_POSTMARKET events
    t1_pm_events = [r for r in first_app if r.get("gap_occurs_in") == "T1_POSTMARKET"]

    # Controls comparison
    ctl_lines = []
    for c in controls:
        ft = c.get("first_trade_wall_clock_et", "?")
        ft_str = pd.Timestamp(ft).strftime("%H:%M ET") if ft and ft != "?" else "?"
        n_pre = c.get("n_trades_before_scanner", "?")
        ctl_lines.append(
            f"  - {c['ticker']} {c['date']}: first_trade={ft_str}, "
            f"n_pre={n_pre}, failure={c.get('entry_failure_reason')}"
        )

    md = f"""---
tags:
  - type/results
  - domain/backtest
  - project/hawkes-ofi-impact
  - status/complete
created: {pd.Timestamp.now().strftime("%Y-%m-%d")}
phase: DIAG-TAPE
---

# Phase DIAG-TAPE — Pre-Event Price Action: Summary

Read-only data investigation. No code changes, no fix recommendations.

## T-1 data availability

| Metric | Count |
|--------|-------|
| First-appearance events with T-1 PM data present | {n_t1_pm_present} / {len(first_app)} |
| First-appearance events with T-1 PM data absent  | {n_t1_pm_absent} / {len(first_app)} |

### first_trade_wall_clock_et distribution (37 first-appearance events)

| Stat | Value |
|------|-------|
| Earliest | {ft_min} |
| Median   | {ft_med} |
| Latest   | {ft_max} |
| Before 06:00 ET | {ft_before_0600} / {len(first_trades)} |
| Before 09:30 ET (pre-market) | {ft_before_0930} / {len(first_trades)} |

## Gap location breakdown

| Gap occurs in | N | % |
|---------------|---|---|
{chr(10).join(f"| {k} | {v} | {v/len(first_app)*100:.1f}% |"
              for k, v in sorted(gap_counts.items(), key=lambda x: -x[1]))}

Total first-appearance events: {len(first_app)}

{"### T1_POSTMARKET events (gap developed during T-1 post-market)" if t1_pm_events else ""}
{"No T1_POSTMARKET events detected." if not t1_pm_events else ""}
{chr(10).join(f"  - {r['ticker']} {r['date']}: n_t1_pm={r.get('n_trades_t1_postmarket')}, gap_from_t1_close={r.get('gap_from_t1_close', 'N/A'):.1f}%" for r in t1_pm_events) if t1_pm_events else ""}

## What this means for the strategy

Data only. No fix recommendation.

The `gap_occurs_in` distribution shows where the price gap that triggered the scanner
actually occurred relative to the available tape.

- **OVERNIGHT_NO_TAPE**: The stock was already at/above the 30% threshold at its first
  event-day trade. The move happened overnight (or very early pre-market) with no tape
  in the parquet. The scanner hit is the first available tick. The Hawkes engine sees
  zero pre-scanner history, so the anchor and gate cannot warm up before the 300s deadline.

- **T_PREMARKET**: Price rose gradually from near prev_close during T pre-market
  (04:00–09:30 ET). The scanner hit came during pre-market. Whether this gives enough
  pre-scanner trade history for anchor warmup depends on how much pre-market activity occurred.

- **T1_POSTMARKET**: The catalyst was visible in T-1 post-market trading. The scanner
  could in principle have seen this earlier in live trading, but the current design
  evaluates only the event date.

- **UNKNOWN**: Insufficient data to classify.

## Comparison with controls

The 5 traded controls had substantial pre-scanner trade history:

{chr(10).join(ctl_lines)}

Traded controls have early first trades (pre-market or RTH open) with large n_pre-scanner
counts. Their anchors had time to fire and warm up before the scanner hit. This is in
direct contrast to first-appearance events where n_pre=0 by construction.

{"## Errors" if errors else ""}
{chr(10).join(f"  - {e['ticker']} {e['date']}: {e.get('error')}" for e in errors) if errors else ""}
"""

    (OUT / "summary.md").write_text(md, encoding="utf-8")
    print(f"  Wrote {OUT / 'summary.md'}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=== Phase DIAG-TAPE ===")

    # Load entry audit
    with open(AUDIT_PATH, encoding="utf-8") as f:
        audit = json.load(f)
    print(f"Loaded {len(audit)} entries from entry_audit.json")

    # Build event list: 37 first-appearance + 5 controls
    first_app_rows = [r for r in audit if r.get("is_first_appearance")]
    print(f"First-appearance events: {len(first_app_rows)}")

    control_rows = [
        r for r in audit
        if (r["ticker"], r["date"]) in CONTROL_KEYS
    ]
    print(f"Control events found in audit: {len(control_rows)}")
    if len(control_rows) != 5:
        missing = CONTROL_KEYS - {(r["ticker"], r["date"]) for r in control_rows}
        print(f"  WARNING: missing controls: {missing}")

    all_rows = [(r, False) for r in first_app_rows] + [(r, True) for r in control_rows]
    print(f"Total events to process: {len(all_rows)}")

    # ── T1: Analyse all events ──────────────────────────────────────────
    print("\n--- T1: Analysing events ---")
    results = []
    for i, (row, is_ctrl) in enumerate(all_rows):
        tk, dt = row["ticker"], row["date"]
        print(f"  [{i+1:2d}/{len(all_rows)}] {tk} {dt} {'[CTRL]' if is_ctrl else ''}", end=" ... ")
        try:
            ev = analyze_event(row, is_ctrl)
            print(f"gap={ev.get('gap_occurs_in')} t1_pm={ev.get('t1_postmarket_present')} "
                  f"first_trade={ev.get('first_trade_wall_clock_et', '?')[:16] if ev.get('first_trade_wall_clock_et') else '?'}")
        except Exception as e:
            traceback.print_exc()
            ev = {"ticker": tk, "date": dt, "is_control": is_ctrl, "error": str(e), "gap_occurs_in": "UNKNOWN"}
            print(f"ERROR: {e}")
        results.append(ev)

    # Write data_availability.json
    out_json = OUT / "data_availability.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nWrote {out_json} ({len(results)} entries)")

    # ── Escalation check: if ALL T-1 PM absent for first-appearance events ──
    fa_results = [r for r in results if not r.get("is_control") and not r.get("error")]
    n_t1_pm = sum(1 for r in fa_results if r.get("t1_postmarket_present"))
    if n_t1_pm == 0 and len(fa_results) >= 37:
        print("\n*** ESCALATION: T-1 post-market data absent for ALL first-appearance events ***")
        print("    Sample timestamp ranges:")
        for r in fa_results[:5]:
            print(f"      {r['ticker']} {r['date']}: {r.get('earliest_ts_et')} → {r.get('latest_ts_et')}")
        print("    Hard stop — posting to chat and exiting.")
        return results, False

    # ── T2: Generate charts ──────────────────────────────────────────────
    print("\n--- T2: Generating charts ---")
    n_ok = 0
    n_fail = 0
    failed_charts = []

    for i, ev in enumerate(results):
        if ev.get("error"):
            print(f"  [{i+1:2d}] {ev['ticker']} {ev['date']} — SKIPPED (data error: {ev['error']})")
            n_fail += 1
            failed_charts.append((ev["ticker"], ev["date"], ev.get("error", "data_error")))
            continue

        tk   = ev["ticker"]
        date = ev["date"]
        is_ctrl = ev.get("is_control", False)
        gap  = ev.get("gap_occurs_in", "UNKNOWN")
        fname = f"{tk}_{date}_CONTROL.html" if is_ctrl else f"{tk}_{date}_{gap}.html"

        print(f"  [{i+1:2d}/{len(results)}] {fname}", end=" ... ")

        try:
            event_path = find_event_path(tk, date)
            df = load_full_trades(event_path)
            if df is None or len(df) == 0:
                raise ValueError("empty trades dataframe")

            fig = generate_chart(ev, df)
            if fig is None:
                raise ValueError("chart returned None (no OHLC data in window)")

            out_path = CHARTS_DIR / fname
            fig.write_html(str(out_path), include_plotlyjs="cdn")
            print(f"OK ({out_path.stat().st_size // 1024} KB)")
            n_ok += 1
        except Exception as e:
            traceback.print_exc()
            print(f"FAILED: {e}")
            n_fail += 1
            failed_charts.append((tk, date, str(e)))

    print(f"\nCharts: {n_ok} OK, {n_fail} failed")

    # Escalation check
    if n_ok < 35:
        print(f"\n*** ESCALATION: Only {n_ok}/42 charts rendered successfully ***")
        print("Failed charts:")
        for tk, dt, err in failed_charts:
            print(f"  {tk} {dt}: {err}")
        return results, False

    # ── T3: Index ────────────────────────────────────────────────────────
    print("\n--- T3: Writing index.html ---")
    write_index(results)

    # ── T4: Summary ──────────────────────────────────────────────────────
    print("\n--- T4: Writing summary.md ---")
    write_summary(results)

    return results, True


if __name__ == "__main__":
    results, ok = main()

    # ── Reporting ────────────────────────────────────────────────────────
    from collections import Counter

    fa = [r for r in results if not r.get("is_control") and not r.get("error")]
    ctrl = [r for r in results if r.get("is_control") and not r.get("error")]

    print("\n" + "="*60)
    print("REPORTING")
    print("="*60)

    # T1a: gap_occurs_in distribution
    gap_counts = Counter(r.get("gap_occurs_in", "UNKNOWN") for r in fa)
    print(f"\nT1a — gap_occurs_in for {len(fa)} first-appearance events:")
    for cat in ["T1_POSTMARKET", "OVERNIGHT_NO_TAPE", "T_PREMARKET", "UNKNOWN"]:
        n = gap_counts.get(cat, 0)
        pct = n / len(fa) * 100 if fa else 0
        print(f"  {cat:<22} N={n:2d}  {pct:.1f}%")

    # T1a: T-1 PM presence
    n_t1_pm = sum(1 for r in fa if r.get("t1_postmarket_present"))
    print(f"\n  T-1 PM data present: {n_t1_pm}/{len(fa)} events")

    # T1b: first_trade_wall_clock_et distribution
    first_trades = []
    for r in fa:
        ft = r.get("first_trade_wall_clock_et")
        if ft:
            try:
                first_trades.append(pd.Timestamp(ft))
            except Exception:
                pass
    first_trades_sorted = sorted(first_trades, key=lambda t: (t.hour, t.minute, t.second))
    if first_trades_sorted:
        ft_min = first_trades_sorted[0].strftime("%H:%M ET")
        ft_med = first_trades_sorted[len(first_trades_sorted)//2].strftime("%H:%M ET")
        ft_max = first_trades_sorted[-1].strftime("%H:%M ET")
        ft_0600 = sum(1 for t in first_trades if t.hour < 6)
        ft_0930 = sum(1 for t in first_trades
                      if t.hour < 9 or (t.hour == 9 and t.minute < 30))
        print(f"\nT1b — first_trade_wall_clock_et distribution ({len(first_trades)} events):")
        print(f"  Earliest: {ft_min}")
        print(f"  Median:   {ft_med}")
        print(f"  Latest:   {ft_max}")
        print(f"  Before 06:00 ET: {ft_0600}")
        print(f"  Before 09:30 ET: {ft_0930}")

    # T2b: T1_POSTMARKET events
    t1_pm_events = [r for r in fa if r.get("gap_occurs_in") == "T1_POSTMARKET"]
    if t1_pm_events:
        print(f"\nT2b — T1_POSTMARKET events ({len(t1_pm_events)}):")
        for r in t1_pm_events:
            print(f"  {r['ticker']} {r['date']}: "
                  f"n_t1_pm={r.get('n_trades_t1_postmarket')}, "
                  f"gap_from_t1_close={r.get('gap_from_t1_close', 'N/A'):.1f}%")
    else:
        print("\nT2b — No T1_POSTMARKET events detected.")

    # Controls comparison
    print(f"\nControls ({len(ctrl)} events):")
    for c in ctrl:
        ft = c.get("first_trade_wall_clock_et", "?")
        ft_str = pd.Timestamp(ft).strftime("%H:%M ET") if ft and ft != "?" else "?"
        n_pre = c.get("n_trades_before_scanner", "?")
        print(f"  {c['ticker']} {c['date']}: first_trade={ft_str}, "
              f"n_pre={n_pre}, gap={c.get('gap_occurs_in')}")

    # Output files
    print("\nOutput files:")
    for p in [
        OUT / "data_availability.json",
        CHARTS_DIR / "index.html",
        OUT / "summary.md",
    ]:
        size_kb = p.stat().st_size // 1024 if p.exists() else -1
        status = f"OK ({size_kb} KB)" if p.exists() else "MISSING"
        print(f"  {p.relative_to(BACKTEST)}: {status}")

    n_charts = len(list(CHARTS_DIR.glob("*.html"))) - 1  # exclude index.html
    print(f"  results/phase_diag_tape/charts/*.html: {n_charts} charts")
