from __future__ import annotations

import numpy as np

from backtest.setup_filter import SetupFilterResult

Q_THRESHOLD: float = 0.65


def entry_eligible(result: SetupFilterResult, n_hold: int = 3) -> bool:
    """Return True if the last n_hold bars of q_tilde are all >= Q_THRESHOLD.

    Cross-and-hold entry criterion for EPG-Rapid: the gate must have been
    continuously in PASS territory for n_hold consecutive bars before entry.
    """
    q = result.q_tilde
    if len(q) < n_hold:
        return False
    return bool(np.all(q[-n_hold:] >= Q_THRESHOLD))
