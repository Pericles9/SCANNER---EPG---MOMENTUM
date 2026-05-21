"""Phase G v2 momentum-weighted scanner quartile computation."""
from __future__ import annotations


def compute_scanner_context(qualifying: list[dict]) -> list[dict]:
    """
    qualifying: list of dicts with 'ticker' and 'pct_change' keys.
    Returns each dict augmented with scanner_rank, scanner_n, scanner_heat,
    scanner_quartile.

    Phase G v2 momentum-weighted quartile:
      1. Sort descending by pct_change.
      2. total = sum(pct_change for all qualifying names)
      3. Walk accumulating running sum:
         running < total/4    → Q1  (dominant movers)
         running < total/2    → Q2
         running < 3*total/4  → Q3
         else                 → Q4  (secondary)
      Gate in scanner/monitor.py: only trade_quartiles (default [2,3]) pass.
    """
    if not qualifying:
        return []

    sorted_names = sorted(qualifying, key=lambda x: x["pct_change"], reverse=True)
    total = sum(x["pct_change"] for x in sorted_names)
    scanner_n = len(sorted_names)

    results = []
    running = 0.0
    for rank, item in enumerate(sorted_names, start=1):
        running += item["pct_change"]

        if total > 0:
            if running < total / 4:
                quartile = 1
            elif running < total / 2:
                quartile = 2
            elif running < 3 * total / 4:
                quartile = 3
            else:
                quartile = 4
        else:
            quartile = 4

        augmented = dict(item)
        augmented["scanner_rank"] = rank
        augmented["scanner_n"] = scanner_n
        augmented["scanner_heat"] = item["pct_change"] / total if total > 0 else 0.0
        augmented["scanner_quartile"] = quartile
        results.append(augmented)

    return results
