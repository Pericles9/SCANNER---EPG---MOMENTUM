"""
Phase SEB-X v2-VIZ Task 4 -- Per-trade candlestick charts (mplfinance).

Charts a curated set of ~16-20 trades for the primary kept stack (B0+R1+R3_vwap),
plus a --ticker/--date selector for ad-hoc rendering.

Curated set:
  4 biggest winners + 4 biggest losers (by realized_ret_pct)
  1 median trade
  >=2 examples of each exit_reason that actually fires (R1, R3, B0, horizon)
  2-3 random others for unbiased coverage

Each chart (1-min OHLC, window = entry-5 to exit+10 bars):
  - Session VWAP line (blue)
  - Entry marker (green triangle up)
  - Exit marker colored by reason (R1=red, R3=orange, B0=blue, horizon=gray)
  - R1 stop level (horizontal dashed red, when applicable)
  - R3 arm threshold (horizontal dotted green, when applicable)
  - R3 trail level (step orange line, when applicable, once armed)
  - MFE30 level (horizontal dotted lightgreen)
  - MAE30 level (horizontal dotted pink)
  - 30-bar horizon marker (vertical dotted gray)
  - Title: TICKER DATE | reason | ret X.XX% | capture Y.YY | sigma=Z.ZZZ

Gate D: for each chart, assert exit bar satisfies its rule condition in code.

Outputs:
  results/seb_x_v2viz/trades/<TICKER>_<DATE>_<stack>.png
  results/seb_x_v2viz/contact_sheet_<stack>.png
"""
from __future__ import annotations

import logging
import math
import random
import sys
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mplfinance as mpf
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT.parent), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from data.loaders.trades import _session_ns_bounds             # noqa: E402
from setup_filter import _build_1min_bars                      # noqa: E402
from tools.seb.feed import _AVAILABLE_DIRS, load_ticks_for_session  # noqa: E402
from tools.seb.simulator import _compute_vwap_per_bar          # noqa: E402
from tools.seb_x.sweep import _simulate_one                    # noqa: E402

OUTPUT_DIR    = _REPO_ROOT / "results" / "seb_x_v2viz"
TRADES_DIR    = OUTPUT_DIR / "trades"
IN_PARQUET    = OUTPUT_DIR / "per_trade_exits.parquet"
PATHS_PARQUET = _REPO_ROOT / "results" / "seb_x" / "paths.parquet"

PRIMARY_STACK = "B0+R1+R3_vwap"
PRIMARY_CFG   = {"use_b0": True, "use_r1": True, "k1": 2.5, "use_r3": True, "arm_mult": 2.0, "g": 0.5}

_REASON_COLOR = {
    "R1":      "#d62728",   # red
    "R3":      "#ff7f0e",   # orange
    "B0":      "#1f77b4",   # blue
    "horizon": "#7f7f7f",   # gray
}
_PRE_BARS  = 5
_POST_BARS = 10

NS_PER_MIN = 60_000_000_000


def _gate_d_assert(
    ph: np.ndarray, pl: np.ndarray, pc: np.ndarray, pv: np.ndarray,
    entry_price: float, sigma_val: float,
    exit_bar: int, exit_reason: str,
    cfg: dict,
) -> None:
    k1      = cfg.get("k1",       2.0)
    arm_m   = cfg.get("arm_mult", 2.0)
    g       = cfg.get("g",        1.0)
    n       = len(pc)

    if exit_reason == "R1":
        floor = entry_price - k1 * sigma_val
        assert pl[exit_bar] <= floor, (
            f"Gate D R1 FAIL: low={pl[exit_bar]:.4f} > floor={floor:.4f}"
        )
    elif exit_reason == "R3":
        arm_level    = entry_price + arm_m * sigma_val
        running_peak = np.maximum.accumulate(ph)
        trail        = running_peak[exit_bar] - g * sigma_val
        assert running_peak[exit_bar] >= arm_level, (
            f"Gate D R3 FAIL: peak={running_peak[exit_bar]:.4f} < arm={arm_level:.4f}"
        )
        assert pl[exit_bar] <= trail, (
            f"Gate D R3 FAIL: low={pl[exit_bar]:.4f} > trail={trail:.4f}"
        )
    elif exit_reason == "B0":
        assert pc[exit_bar] < pv[exit_bar], (
            f"Gate D B0 FAIL: close={pc[exit_bar]:.4f} >= vwap={pv[exit_bar]:.4f}"
        )
    elif exit_reason == "horizon":
        assert exit_bar == n - 1, (
            f"Gate D horizon FAIL: exit_bar={exit_bar} but n-1={n-1}"
        )


def _load_session_bars(
    ticker: str, date: str
) -> Optional[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
    """Load 1-min OHLCVWAP bars for a session from tick data.
    Returns (opens, highs, lows, closes, volumes, vwap, bar_starts_ns) or None.
    """
    if (ticker, date) not in _AVAILABLE_DIRS:
        log.warning("No available dir for %s %s", ticker, date)
        return None
    tick_data = load_ticks_for_session(ticker, date)
    if tick_data is None:
        return None
    ts, prices, sizes = tick_data
    sess_start, sess_end = _session_ns_bounds(date)
    mask = (ts >= sess_start) & (ts <= sess_end)
    ts, prices, sizes = ts[mask], prices[mask], sizes[mask]
    if len(ts) == 0:
        return None
    opens, highs, lows, closes, volumes, dvols, bar_starts = _build_1min_bars(
        ts, prices, sizes, sess_start, sess_end
    )
    vwap = _compute_vwap_per_bar(bar_starts, dvols, volumes, sess_start)
    return opens, highs, lows, closes, volumes, vwap, bar_starts


def _r3_trail_series(
    path_h: np.ndarray, entry_price: float, sigma_val: float,
    arm_mult: float, g: float, n_window: int, entry_idx_in_window: int,
) -> np.ndarray:
    """Compute the R3 trail level for each bar in the chart window.
    Returns array of shape (n_window,) with NaN before arm triggers.
    """
    arm_level    = entry_price + arm_mult * sigma_val
    running_peak = np.maximum.accumulate(path_h)
    trail        = running_peak - g * sigma_val
    armed        = running_peak >= arm_level

    series = np.full(n_window, np.nan)
    for j, ph_idx in enumerate(range(len(path_h))):
        win_idx = entry_idx_in_window + j
        if win_idx < 0 or win_idx >= n_window:
            continue
        if armed[ph_idx]:
            series[win_idx] = trail[ph_idx]
    return series


def chart_one_trade(
    ticker: str, date: str, stack_label: str, cfg: dict,
    entry_price: float, exit_bar: int, exit_price: float, exit_reason: str,
    sigma_val: float, mfe30_pct: float, mae30_pct: float, capture30: float,
    realized_ret_pct: float,
    path_h: np.ndarray, path_l: np.ndarray, path_c: np.ndarray, path_v: np.ndarray,
    entry_bar_session: int,
    out_path: Path,
) -> bool:
    """Render one trade to a PNG. Returns True on success."""
    bars = _load_session_bars(ticker, date)
    if bars is None:
        log.warning("Cannot load bars for %s %s — skipping chart", ticker, date)
        return False

    opens, highs, lows, closes, volumes, vwap, bar_starts = bars
    n_session = len(opens)

    # Chart window indices in the session bar array
    win_start = max(0, entry_bar_session - _PRE_BARS)
    exit_bar_session = entry_bar_session + exit_bar
    win_end   = min(n_session, exit_bar_session + _POST_BARS + 1)

    if win_end <= win_start:
        log.warning("Empty window for %s %s", ticker, date)
        return False

    # Indices within the window
    entry_idx_in_win = entry_bar_session - win_start
    exit_idx_in_win  = exit_bar_session - win_start

    win_h   = highs[win_start:win_end]
    win_l   = lows[win_start:win_end]
    win_o   = opens[win_start:win_end]
    win_c   = closes[win_start:win_end]
    win_v   = volumes[win_start:win_end]
    win_vwap = vwap[win_start:win_end]
    win_bs  = bar_starts[win_start:win_end]
    n_win   = len(win_h)

    # Build timestamp index (Eastern Time)
    try:
        ts_utc = pd.to_datetime(win_bs, unit="ns", utc=True)
        ts_et  = ts_utc.tz_convert("America/New_York")
    except Exception:
        ts_et  = pd.date_range(start="09:30", periods=n_win, freq="1min", tz="America/New_York")

    ohlcv = pd.DataFrame({
        "Open":   win_o,
        "High":   win_h,
        "Low":    win_l,
        "Close":  win_c,
        "Volume": win_v,
    }, index=ts_et)

    # --- Gate D assertion (using path arrays) ---
    use_r1  = cfg.get("use_r1",  False)
    use_r3  = cfg.get("use_r3",  False)
    k1      = cfg.get("k1",      2.0)
    arm_mult = cfg.get("arm_mult", 2.0)
    g_trail  = cfg.get("g",       1.0)
    try:
        _gate_d_assert(path_h, path_l, path_c, path_v,
                       entry_price, sigma_val, exit_bar, exit_reason, cfg)
        gate_d_ok = True
    except AssertionError as exc:
        log.error("Gate D FAIL %s %s: %s", ticker, date, exc)
        gate_d_ok = False

    # --- Build addplot series ---
    apds = []

    # VWAP
    vwap_s = pd.Series(win_vwap, index=ts_et)
    apds.append(mpf.make_addplot(vwap_s, color="steelblue", linewidths=1.5, panel=0))

    # R1 floor
    if use_r1 and sigma_val > 0:
        r1_floor_val = entry_price - k1 * sigma_val
        r1_floor_s   = pd.Series(np.where(
            np.arange(n_win) >= entry_idx_in_win, r1_floor_val, np.nan
        ), index=ts_et)
        apds.append(mpf.make_addplot(r1_floor_s, color="#d62728", linewidths=1.2,
                                     linestyle="dashed", panel=0))

    # R3 arm level and trail
    if use_r3 and sigma_val > 0:
        arm_level_val = entry_price + arm_mult * sigma_val
        arm_s = pd.Series(np.where(
            np.arange(n_win) >= entry_idx_in_win, arm_level_val, np.nan
        ), index=ts_et)
        apds.append(mpf.make_addplot(arm_s, color="#2ca02c", linewidths=1.0,
                                     linestyle="dotted", panel=0))

        trail_arr = _r3_trail_series(
            path_h, entry_price, sigma_val, arm_mult, g_trail, n_win, entry_idx_in_win
        )
        trail_s = pd.Series(trail_arr, index=ts_et)
        if not trail_s.isna().all():
            apds.append(mpf.make_addplot(trail_s, color="#ff7f0e", linewidths=1.5,
                                         panel=0))

    # MFE30 and MAE30 reference levels
    mfe30_price = entry_price * (1.0 + mfe30_pct)
    mae30_price = entry_price * (1.0 + mae30_pct)
    mfe30_s = pd.Series(np.where(
        np.arange(n_win) >= entry_idx_in_win, mfe30_price, np.nan
    ), index=ts_et)
    mae30_s = pd.Series(np.where(
        np.arange(n_win) >= entry_idx_in_win, mae30_price, np.nan
    ), index=ts_et)
    apds.append(mpf.make_addplot(mfe30_s, color="#2ca02c", linewidths=0.8,
                                 linestyle="dotted", panel=0, alpha=0.5))
    apds.append(mpf.make_addplot(mae30_s, color="#d62728", linewidths=0.8,
                                 linestyle="dotted", panel=0, alpha=0.5))

    # Entry / exit scatter markers
    entry_mkr = pd.Series(np.nan, index=ts_et)
    exit_mkr  = pd.Series(np.nan, index=ts_et)
    if 0 <= entry_idx_in_win < n_win:
        entry_mkr.iloc[entry_idx_in_win] = entry_price
    if 0 <= exit_idx_in_win < n_win:
        exit_mkr.iloc[exit_idx_in_win] = exit_price

    apds.append(mpf.make_addplot(entry_mkr, type="scatter", markersize=150,
                                 marker="^", color="#2ca02c", panel=0))
    exit_color = _REASON_COLOR.get(exit_reason, "black")
    apds.append(mpf.make_addplot(exit_mkr, type="scatter", markersize=150,
                                 marker="v", color=exit_color, panel=0))

    # Title
    gate_d_marker = "" if gate_d_ok else " [Gate D FAIL]"
    title = (
        f"{ticker} {date} | {stack_label}{gate_d_marker}\n"
        f"exit={exit_reason}  ret={realized_ret_pct*100:.2f}%  "
        f"capture={capture30:.3f}  σ={sigma_val:.4f}  "
        f"MFE30={mfe30_pct*100:.2f}%"
    )

    try:
        fig, axes = mpf.plot(
            ohlcv,
            type="candle",
            addplot=apds,
            returnfig=True,
            style="yahoo",
            figsize=(14, 7),
            title=title,
            warn_too_much_data=500,
        )
        # 30-bar horizon marker (vertical line at entry+30 bars in window)
        ax = axes[0]
        horizon_idx = entry_idx_in_win + 30
        if 0 <= horizon_idx < n_win:
            ax.axvline(horizon_idx, color="gray", linewidth=0.8, linestyle=":",
                       alpha=0.5)

        # Legend patch for overlays
        from matplotlib.lines import Line2D
        legend_elems = [
            Line2D([0], [0], color="steelblue", lw=1.5, label="VWAP"),
        ]
        if use_r1:
            legend_elems.append(Line2D([0], [0], color="#d62728", lw=1.2, ls="--", label=f"R1 floor k1={k1}σ"))
        if use_r3:
            legend_elems.append(Line2D([0], [0], color="#2ca02c", lw=1.0, ls=":", label=f"R3 arm {arm_mult}σ"))
            legend_elems.append(Line2D([0], [0], color="#ff7f0e", lw=1.5, label=f"R3 trail g={g_trail}σ"))
        legend_elems += [
            Line2D([0], [0], color="#2ca02c", lw=0.8, ls=":", alpha=0.5, label="MFE30"),
            Line2D([0], [0], color="#d62728", lw=0.8, ls=":", alpha=0.5, label="MAE30"),
        ]
        ax.legend(handles=legend_elems, loc="upper left", fontsize=7, framealpha=0.7)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(out_path), dpi=120, bbox_inches="tight")
        plt.close(fig)
        return True
    except Exception as exc:
        log.error("mpf.plot failed for %s %s: %s", ticker, date, exc)
        return False


def _select_curated(df_stack: pd.DataFrame, df_paths: pd.DataFrame, stack_label: str) -> pd.DataFrame:
    """Select ~16-20 curated trades for the given stack."""
    seed = 42
    rng  = random.Random(seed)

    selected_keys: set[tuple] = set()
    selected_rows: list[int]  = []

    def _add_if_new(idx: int) -> None:
        row = df_stack.loc[idx]
        key = (row["ticker"], row["date"])
        if key not in selected_keys:
            selected_keys.add(key)
            selected_rows.append(idx)

    # 4 biggest winners / losers
    for idx in df_stack["realized_ret_pct"].nlargest(4).index:
        _add_if_new(idx)
    for idx in df_stack["realized_ret_pct"].nsmallest(4).index:
        _add_if_new(idx)

    # Median trade
    median_val = df_stack["realized_ret_pct"].median()
    closest    = (df_stack["realized_ret_pct"] - median_val).abs().idxmin()
    _add_if_new(closest)

    # >=2 examples of each exit_reason
    for reason in ["R1", "R3", "B0", "horizon"]:
        candidates = df_stack[df_stack["exit_reason"] == reason].index.tolist()
        if not candidates:
            continue
        rng.shuffle(candidates)
        added = 0
        for idx in candidates:
            if df_stack.loc[idx, "date"][:4] not in ("2020", "2024"):  # prefer mid-sample
                _add_if_new(idx)
                added += 1
                if added >= 2:
                    break
        if added < 2:
            for idx in candidates:
                _add_if_new(idx)
                added += 1
                if added >= 2:
                    break

    # 2-3 random others (from different years)
    remaining = [i for i in df_stack.index if (df_stack.loc[i, "ticker"], df_stack.loc[i, "date"])
                 not in selected_keys]
    rng.shuffle(remaining)
    for idx in remaining[:3]:
        _add_if_new(idx)

    result = df_stack.loc[selected_rows].copy()
    log.info("Curated set for %s: %d trades", stack_label, len(result))
    return result


def chart_curated(
    df_trades: pd.DataFrame,
    df_paths: pd.DataFrame,
    stack_label: str,
    cfg: dict,
) -> list[Path]:
    df_stack = df_trades[df_trades["stack"] == stack_label].reset_index(drop=True)
    curated  = _select_curated(df_stack.set_index(df_stack.index), df_paths, stack_label)

    # Build paths lookup
    path_lookup: dict[tuple, dict] = {}
    for _, row in df_paths.iterrows():
        key = (row["ticker"], row["date"])
        path_lookup[key] = {
            "path_h":          np.array(row["path_high"],  dtype=np.float64),
            "path_l":          np.array(row["path_low"],   dtype=np.float64),
            "path_c":          np.array(row["path_close"], dtype=np.float64),
            "path_v":          np.array(row["path_vwap"],  dtype=np.float64),
            "entry_bar":       int(row["entry_bar"]),
        }

    png_paths: list[Path] = []
    for _, row in curated.iterrows():
        ticker = row["ticker"]
        date   = row["date"]
        key    = (ticker, date)
        pdata  = path_lookup.get(key)
        if pdata is None:
            log.warning("No path data for %s %s", ticker, date)
            continue

        out_name = f"{ticker}_{date}_{_sanitize(stack_label)}.png"
        out_path = TRADES_DIR / out_name

        ok = chart_one_trade(
            ticker=ticker, date=date,
            stack_label=stack_label, cfg=cfg,
            entry_price=float(row["entry_price"]),
            exit_bar=int(row["exit_bar"]),
            exit_price=float(row["exit_price"]),
            exit_reason=str(row["exit_reason"]),
            sigma_val=float(row["sigma_val"]),
            mfe30_pct=float(row["mfe30_pct"]),
            mae30_pct=float(row["mae30_pct"]),
            capture30=float(row["capture30"]) if not math.isnan(row["capture30"]) else 0.0,
            realized_ret_pct=float(row["realized_ret_pct"]),
            path_h=pdata["path_h"],
            path_l=pdata["path_l"],
            path_c=pdata["path_c"],
            path_v=pdata["path_v"],
            entry_bar_session=pdata["entry_bar"],
            out_path=out_path,
        )
        if ok:
            png_paths.append(out_path)
            log.info("Saved %s", out_path.name)

    return png_paths


def _sanitize(label: str) -> str:
    return label.replace("+", "_").replace(" ", "_")


def make_contact_sheet(png_paths: list[Path], out_path: Path, cols: int = 4) -> None:
    if not png_paths:
        return
    try:
        from PIL import Image
        imgs = []
        for p in png_paths:
            try:
                imgs.append(Image.open(str(p)).convert("RGB"))
            except Exception as e:
                log.warning("Cannot open %s: %s", p, e)
        if not imgs:
            return
        w, h    = imgs[0].size
        n_rows  = math.ceil(len(imgs) / cols)
        sheet_w = cols * w
        sheet_h = n_rows * h
        # Limit total sheet size
        scale = min(1.0, 4000 / max(sheet_w, sheet_h))
        if scale < 1.0:
            w, h = int(w * scale), int(h * scale)
            imgs = [img.resize((w, h), Image.LANCZOS) for img in imgs]
            sheet_w, sheet_h = cols * w, n_rows * h

        sheet = Image.new("RGB", (sheet_w, sheet_h), color=(240, 240, 240))
        for i, img in enumerate(imgs):
            r, c = divmod(i, cols)
            sheet.paste(img, (c * w, r * h))
        sheet.save(str(out_path))
        log.info("Wrote contact sheet %s (%dx%d)", out_path.name, sheet_w, sheet_h)
    except ImportError:
        log.warning("PIL not available — skipping contact sheet")
    except Exception as exc:
        log.error("Contact sheet error: %s", exc)


def render_one(ticker: str, date: str, stack_label: str = PRIMARY_STACK) -> None:
    """Ad-hoc: chart a single trade. Called from --ticker/--date CLI."""
    df_trades = pd.read_parquet(str(IN_PARQUET))
    df_paths  = pd.read_parquet(str(PATHS_PARQUET))

    row = df_trades[(df_trades["ticker"] == ticker) &
                    (df_trades["date"]   == date) &
                    (df_trades["stack"]  == stack_label)]
    if row.empty:
        log.error("No trade found for ticker=%s date=%s stack=%s", ticker, date, stack_label)
        return
    row = row.iloc[0]

    p_row = df_paths[(df_paths["ticker"] == ticker) & (df_paths["date"] == date)]
    if p_row.empty:
        log.error("No path data for %s %s", ticker, date)
        return
    p_row = p_row.iloc[0]

    cfg = PRIMARY_CFG if stack_label == PRIMARY_STACK else {"use_b0": True}
    out_path = TRADES_DIR / f"{ticker}_{date}_{_sanitize(stack_label)}.png"
    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    ok = chart_one_trade(
        ticker=ticker, date=date,
        stack_label=stack_label, cfg=cfg,
        entry_price=float(row["entry_price"]),
        exit_bar=int(row["exit_bar"]),
        exit_price=float(row["exit_price"]),
        exit_reason=str(row["exit_reason"]),
        sigma_val=float(row["sigma_val"]),
        mfe30_pct=float(row["mfe30_pct"]),
        mae30_pct=float(row["mae30_pct"]),
        capture30=float(row["capture30"]) if not math.isnan(row["capture30"]) else 0.0,
        realized_ret_pct=float(row["realized_ret_pct"]),
        path_h=np.array(p_row["path_high"],  dtype=np.float64),
        path_l=np.array(p_row["path_low"],   dtype=np.float64),
        path_c=np.array(p_row["path_close"], dtype=np.float64),
        path_v=np.array(p_row["path_vwap"],  dtype=np.float64),
        entry_bar_session=int(p_row["entry_bar"]),
        out_path=out_path,
    )
    if ok:
        print(f"Saved: {out_path}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    df_trades = pd.read_parquet(str(IN_PARQUET))
    df_paths  = pd.read_parquet(str(PATHS_PARQUET))
    log.info("Loaded trades=%d paths=%d", len(df_trades), len(df_paths))

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    TRADES_DIR.mkdir(parents=True, exist_ok=True)

    for stack_label, cfg in [
        (PRIMARY_STACK, PRIMARY_CFG),
        ("B0",          {"use_b0": True}),
    ]:
        log.info("=== Charting curated set for stack: %s ===", stack_label)
        png_paths = chart_curated(df_trades, df_paths, stack_label, cfg)

        if png_paths:
            sheet_path = OUTPUT_DIR / f"contact_sheet_{_sanitize(stack_label)}.png"
            make_contact_sheet(png_paths, sheet_path, cols=4)

    log.info("Task 4 complete.")


if __name__ == "__main__":
    main()
