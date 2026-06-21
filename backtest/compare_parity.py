#!/usr/bin/env python3
"""T3 parity comparison: classic runner.py vs runner_rapid.py parity mode.

Usage:
    python -m backtest.compare_parity \\
        --classic-dir backtest/results/phase_r0/classic \\
        --parity-dir  backtest/results/phase_r0/parity \\
        --out         backtest/results/phase_r0/parity_diff.json

Exit code 0 = clean parity (n_diffs == 0).
Exit code 1 = divergence detected — hard stop before R1.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_trades(results_dir: Path) -> list[dict]:
    p = results_dir / "per_trade.json"
    with open(p) as f:
        return json.load(f)


def compare(classic_trades: list[dict], parity_trades: list[dict]) -> dict:
    COMPARE_FIELDS = [
        "entry_ts", "exit_ts", "entry_price", "exit_price",
        "pnl_pct", "hold_sec", "exit_reason",
    ]

    def _key(t):
        return (t["ticker"], t["date"], int(t.get("trade_seq", 0)))

    ct_map = {_key(t): t for t in classic_trades}
    pt_map = {_key(t): t for t in parity_trades}

    all_keys = sorted(set(ct_map) | set(pt_map))
    diffs = []
    for k in all_keys:
        ct = ct_map.get(k)
        pt = pt_map.get(k)
        if ct is None:
            diffs.append({"key": str(k), "issue": "missing_in_classic"})
        elif pt is None:
            diffs.append({"key": str(k), "issue": "missing_in_parity"})
        else:
            fd = {}
            for f in COMPARE_FIELDS:
                cv, pv = ct.get(f), pt.get(f)
                if isinstance(cv, float) and isinstance(pv, float):
                    if abs(cv - pv) > 1e-9:
                        fd[f] = {"classic": cv, "parity": pv}
                elif cv != pv:
                    fd[f] = {"classic": cv, "parity": pv}
            if fd:
                diffs.append({"key": str(k), "field_diffs": fd})

    return {
        "n_classic_trades": len(classic_trades),
        "n_parity_trades": len(parity_trades),
        "n_diffs": len(diffs),
        "diffs": diffs[:100],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--classic-dir", required=True)
    parser.add_argument("--parity-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    classic_trades = load_trades(Path(args.classic_dir))
    parity_trades = load_trades(Path(args.parity_dir))

    result = compare(classic_trades, parity_trades)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        json.dump(result, f, indent=2)

    n = result["n_diffs"]
    print(f"Classic: {result['n_classic_trades']} trades")
    print(f"Parity:  {result['n_parity_trades']} trades")
    print(f"Diffs:   {n}")

    if n == 0:
        print("T3a PASS — parity confirmed, diff is empty.")
        return 0
    else:
        print(f"T3a FAIL — {n} divergences. HARD STOP before R1.")
        for d in result["diffs"][:5]:
            print(f"  {d}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
