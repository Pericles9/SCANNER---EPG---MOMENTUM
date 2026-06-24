"""
Phase SEB-X v2-VIZ Task 1 -- Per-trade realized exits for the three visualization stacks.

Reuses _simulate_one from tools.seb_x.sweep (no new exit logic).

Stacks:
  B0              : VWAP-cross baseline (sigma_unit=vwap, but sigma unused by B0)
  B0+R1+R3 vwap   : kept stack under sigma_vwap (k1=2.5 arm=2.0 g=0.5)
  B0+R1+R3 prim   : same params under sigma_final (sigma_primary + ADR floor)

Gate A: MFE(5/15/30) medians from paths.parquet match reference +/-15%.
Gate B: exit_reason breakdown per stack; flag if any reason is ~0% or ~100%.

Output: results/seb_x_v2viz/per_trade_exits.parquet
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT.parent), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.seb_x.sweep import _simulate_one  # noqa: E402

PATHS_PARQUET = _REPO_ROOT / "results" / "seb_x"     / "paths.parquet"
SIGMA_PARQUET = _REPO_ROOT / "results" / "seb_x_v2"  / "sigma_context.parquet"
OUTPUT_DIR    = _REPO_ROOT / "results" / "seb_x_v2viz"
OUT_PARQUET   = OUTPUT_DIR / "per_trade_exits.parquet"

# Gate A reference medians (from actual paths.parquet)
_GATE_A_MFE = {"5m": 0.03472, "15m": 0.05678, "30m": 0.07202}
_GATE_A_TOL = 0.15  # relative tolerance

NS_PER_MIN = 60_000_000_000

STACKS: list[dict] = [
    {
        "label":      "B0",
        "sigma_unit": "vwap",
        "cfg":        {"use_b0": True},
    },
    {
        "label":      "B0+R1+R3_vwap",
        "sigma_unit": "vwap",
        "cfg":        {
            "use_b0": True, "use_r1": True, "k1": 2.5,
            "use_r3": True, "arm_mult": 2.0, "g": 0.5,
        },
    },
    {
        "label":      "B0+R1+R3_prim",
        "sigma_unit": "primary",
        "cfg":        {
            "use_b0": True, "use_r1": True, "k1": 2.5,
            "use_r3": True, "arm_mult": 2.0, "g": 0.5,
        },
    },
]


def _gate_a(df_paths: pd.DataFrame) -> None:
    keys = {"5m": "mfe_5m_rc", "15m": "mfe_15m_rc", "30m": "mfe_30m_rc"}
    all_pass = True
    for label, col in keys.items():
        actual = float(df_paths[col].median())
        ref    = _GATE_A_MFE[label]
        rel    = abs(actual - ref) / ref
        ok     = rel <= _GATE_A_TOL
        log.info("Gate A MFE(%s): actual=%.4f%% ref=%.4f%% rel_err=%.1f%% -> %s",
                 label, actual * 100, ref * 100, rel * 100, "PASS" if ok else "FAIL")
        if not ok:
            all_pass = False
    if not all_pass:
        raise RuntimeError("Gate A FAIL — MFE medians diverged from reference")


def apply_stacks(
    df_paths: pd.DataFrame,
    df_sigma: pd.DataFrame,
    stacks: list[dict] | None = None,
) -> pd.DataFrame:
    """Apply stacks to all entries. Returns per-trade DataFrame (n=990 × n_stacks rows)."""
    if stacks is None:
        stacks = STACKS

    sig_sel = df_sigma[["ticker", "date", "sigma_final", "sigma_vwap", "sigma_source"]].rename(
        columns={"sigma_source": "sigma_source_v2"}
    )
    df = pd.merge(df_paths, sig_sel, on=["ticker", "date"], how="inner")
    n  = len(df)
    if n != len(df_paths):
        log.warning("Merge dropped %d rows (paths=%d sigma=%d merged=%d)",
                    len(df_paths) - n, len(df_paths), len(df_sigma), n)

    all_ph  = [np.array(r, dtype=np.float64) for r in df["path_high"]]
    all_pl  = [np.array(r, dtype=np.float64) for r in df["path_low"]]
    all_pc  = [np.array(r, dtype=np.float64) for r in df["path_close"]]
    all_pv  = [np.array(r, dtype=np.float64) for r in df["path_vwap"]]

    entry_prices    = df["entry_price"].values.astype(np.float64)
    mfe_30m         = df["mfe_30m_rc"].values.astype(np.float64)
    mae_30m         = df["mae_30m_rc"].values.astype(np.float64)
    entry_ts_ns     = df["entry_ts_ns"].values.astype(np.int64)
    tickers         = df["ticker"].values
    dates           = df["date"].values
    session_buckets = df["session_bucket"].values
    is_event_days   = df["is_event_day"].values
    sigma_final_arr = df["sigma_final"].values.astype(np.float64)
    sigma_vwap_arr  = df["sigma_vwap"].values.astype(np.float64)
    sigma_sources   = df["sigma_source_v2"].values

    rows: list[dict] = []
    for stack in stacks:
        cfg    = stack["cfg"]
        label  = stack["label"]
        sigmas = sigma_vwap_arr if stack["sigma_unit"] == "vwap" else sigma_final_arr

        log.info("Applying stack '%s' to %d entries ...", label, n)
        for i in range(n):
            exit_bar, exit_price, exit_reason = _simulate_one(
                all_ph[i], all_pl[i], all_pc[i], all_pv[i],
                entry_prices[i], sigmas[i],
                use_b0   = cfg.get("use_b0",   False),
                use_r1   = cfg.get("use_r1",   False), k1       = cfg.get("k1",       2.0),
                use_r2   = cfg.get("use_r2",   False), t_dead   = cfg.get("t_dead",   15),
                a        = cfg.get("a",         0.0),
                use_r3   = cfg.get("use_r3",   False), arm_mult = cfg.get("arm_mult", 2.0),
                g        = cfg.get("g",         1.0),
                use_tpsl = cfg.get("use_tpsl", False), k_tp     = cfg.get("k_tp",     3.0),
                k_sl     = cfg.get("k_sl",      1.5),
            )
            ep   = entry_prices[i]
            xp   = exit_price
            sig  = sigmas[i]
            mfe  = mfe_30m[i]
            mae  = mae_30m[i]
            ret  = (xp - ep) / ep

            rows.append({
                "stack":              label,
                "ticker":             tickers[i],
                "date":               dates[i],
                "year":               int(str(dates[i])[:4]),
                "session_bucket":     session_buckets[i],
                "is_event_day":       bool(is_event_days[i]),
                "entry_ts_ns":        int(entry_ts_ns[i]),
                "entry_price":        float(ep),
                "exit_bar":           int(exit_bar),
                "exit_ts_ns":         int(entry_ts_ns[i]) + int(exit_bar) * NS_PER_MIN,
                "exit_price":         float(xp),
                "exit_reason":        exit_reason,
                "realized_ret_pct":   float(ret),
                # dollar-PnL / sigma_dollar — consistent with how stops are calibrated
                "realized_ret_sigma": float((xp - ep) / sig) if sig > 0 else float("nan"),
                "mfe30_pct":          float(mfe),
                "mae30_pct":          float(mae),
                "capture30":          float(ret / mfe) if mfe > 0 else float("nan"),
                "sigma_final":        float(sigma_final_arr[i]),
                "sigma_vwap":         float(sigma_vwap_arr[i]),
                "sigma_val":          float(sig),
                "sigma_source":       str(sigma_sources[i]),
            })

    return pd.DataFrame(rows)


def _gate_b(df: pd.DataFrame) -> None:
    for label in df["stack"].unique():
        sub = df[df["stack"] == label]
        n   = len(sub)
        log.info("Gate B [%s] n=%d:", label, n)
        counts = sub["exit_reason"].value_counts()
        for reason, cnt in counts.items():
            pct  = 100.0 * cnt / n
            flag = "  *** FLAG" if pct >= 99.0 or pct <= 0.5 else ""
            log.info("  %-12s %4d  (%5.1f%%)%s", reason, cnt, pct, flag)


def main() -> pd.DataFrame:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    df_paths = pd.read_parquet(str(PATHS_PARQUET))
    df_sigma = pd.read_parquet(str(SIGMA_PARQUET))
    log.info("Loaded paths=%d sigma=%d", len(df_paths), len(df_sigma))

    _gate_a(df_paths)

    df_out = apply_stacks(df_paths, df_sigma)
    _gate_b(df_out)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    df_out.to_parquet(str(OUT_PARQUET), index=False)
    log.info("Wrote %s (%d rows across %d stacks)", OUT_PARQUET, len(df_out), len(STACKS))
    return df_out


if __name__ == "__main__":
    main()
