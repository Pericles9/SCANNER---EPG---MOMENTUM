"""LULD V3 scoring — confusion-matrix evaluation of LULD exit fires vs halt labels.

Implements the Phase LULD-V3 objective function. Scores proximity-exit fires
against ground-truth halt windows produced by luld_halt_detection.detect_luld_halts().

Scoring rules
-------------
TP  fire occurs within pre_halt_window_sec BEFORE a halt start:
        0  ≤  halt.start_sec - fire.timestamp_sec  ≤  pre_halt_window_sec

FP  fire has no halt starting within pre_halt_window_sec after it

FN  halt start has no fire within pre_halt_window_sec before it

A single fire can be TP for at most one halt (earliest eligible halt wins).
A single halt can match at most one fire (latest eligible fire wins, i.e. the
closest precursor is preferred).

Liquidity penalty (per fire)
-----------------------------
shares_needed  = position_value_usd / mid_price
liq_penalty    = spread_bps  if bid_size_shares < shares_needed  else  0.0

Composite score (higher is better)
------------------------------------
score = w_recall * recall - w_fp * fp_rate - w_liq * mean_liq_penalty

Default weights: w_recall=3.0, w_fp=1.0, w_liq=1.0
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class FireEvent:
    """A single EXIT_HALT signal emitted by the proximity module."""
    timestamp_ns: int        # unix nanoseconds (UTC)
    spread_bps: float        # (ask - bid) / mid * 10_000 at fire tick
    bid_size_shares: float   # prevailing NBBO bid size in shares
    mid_price: float         # (bid + ask) / 2 at fire tick; used for shares_needed


@dataclass
class HaltLabel:
    """Ground-truth halt window from the halt labeler (mirrors HaltWindow)."""
    start_sec: float    # seg_end: last in-band trade before the gap (== HaltWindow.start)
    end_sec: float      # halt/gap end as seconds since epoch
    reason: str = "luld"
    limit_state_start_sec: Optional[float] = None  # seg_start: limit-state onset (Phase LULD-V3c)


@dataclass
class EventScore:
    """Per-event scoring result."""
    n_fires: int
    n_halts: int
    tp: int
    fp: int
    fn: int
    recall: float          # tp / n_halts     (0.0 if no halts)
    precision: float       # tp / n_fires      (0.0 if no fires)
    fp_rate: float         # fp / n_fires      (0.0 if no fires)
    mean_liq_penalty: float
    composite: float


def _halt_labels_from_halt_windows(halts: list) -> List[HaltLabel]:
    """Convert HaltWindow objects (from luld_halt_detection) to HaltLabel."""
    labels = []
    for h in halts:
        start_sec = float(h.start.timestamp()) if hasattr(h.start, "timestamp") else float(h.start)
        end_sec = float(h.end.timestamp()) if hasattr(h.end, "timestamp") else float(h.end)
        lss = getattr(h, "limit_state_start", None)
        if lss is not None:
            lss = float(lss.timestamp()) if hasattr(lss, "timestamp") else float(lss)
        labels.append(HaltLabel(
            start_sec=start_sec, end_sec=end_sec,
            reason=getattr(h, "reason", "luld"), limit_state_start_sec=lss,
        ))
    return labels


# Liquidity-penalty normalization: the spread (bps) at which fill quality is
# treated as fully impaired. Maps raw spread_bps -> [0, 1] so the w_liq weight is
# commensurate with recall/fp_rate. Phase LULD-V3c T4 fix. Cooper-adjustable.
TARGET_SPREAD_BPS = 100.0


def score_fires(
    fires: List[FireEvent],
    halts: list,
    pre_halt_window_sec: float = 15.0,
    w_recall: float = 3.0,
    w_fp: float = 1.0,
    w_liq: float = 1.0,
    position_value_usd: float = 1000.0,
    target_spread_bps: float = TARGET_SPREAD_BPS,
) -> EventScore:
    """Score a list of fire events against ground-truth halt labels.

    Matching window (Phase LULD-V3c T3 fix)
    ---------------------------------------
    A fire is a true precursor if it lands anywhere in the limit-state window
        [limit_state_start_sec - pre_halt_window_sec, start_sec]
    where ``limit_state_start_sec`` is the limit-state onset (seg_start) and
    ``start_sec`` is seg_end (the freeze moment). Firing anywhere in the pinned
    run, or within ``pre_halt_window_sec`` before its onset, is a timely exit.

    - TP: a halt with >= 1 fire inside its window (the halt is caught).
    - FP: a fire inside no halt's window.
    - FN: a halt with no fire inside its window.
    Multiple fires inside one halt's window count as a single TP and are NOT
    charged as FPs (repeated firing during a real limit state is not a false
    alarm). When ``limit_state_start_sec`` is missing (legacy labels), the window
    degrades to the old [start_sec - pre_halt_window_sec, start_sec] anchor.

    Parameters
    ----------
    fires:
        FireEvent objects from a single-event replay.
    halts:
        Either List[HaltLabel] or List[HaltWindow] from luld_halt_detection.
        HaltWindow objects are converted automatically.
    pre_halt_window_sec:
        Lead time (seconds) before the limit-state onset still counted as timely.
    w_recall, w_fp, w_liq:
        Composite score weights.
    position_value_usd:
        Dollar amount used to compute shares_needed = position_value_usd / mid_price.
    target_spread_bps:
        Spread (bps) treated as fully-impaired liquidity; normalizes the penalty.
    """
    if halts and not isinstance(halts[0], HaltLabel):
        labels = _halt_labels_from_halt_windows(halts)
    else:
        labels: List[HaltLabel] = list(halts)  # type: ignore[assignment]

    n_fires = len(fires)
    n_halts = len(labels)

    if n_fires == 0 and n_halts == 0:
        return EventScore(
            n_fires=0, n_halts=0, tp=0, fp=0, fn=0,
            recall=0.0, precision=0.0, fp_rate=0.0,
            mean_liq_penalty=0.0, composite=0.0,
        )

    fire_ts_sec = [f.timestamp_ns / 1e9 for f in fires]

    # --- Window matching against the limit-state run (V3c T3 fix) ---
    # Window per halt: [onset - pre_halt_window_sec, seg_end].
    windows: List[tuple] = []
    for h in labels:
        onset = h.limit_state_start_sec if h.limit_state_start_sec is not None else h.start_sec
        win_lo = onset - pre_halt_window_sec
        win_hi = h.start_sec  # seg_end
        windows.append((win_lo, win_hi))

    matched_fire_indices: set[int] = set()
    matched_halt_indices: set[int] = set()
    for h_idx, (lo, hi) in enumerate(windows):
        caught = False
        for fi, f_ts in enumerate(fire_ts_sec):
            if lo <= f_ts <= hi:
                matched_fire_indices.add(fi)
                caught = True
        if caught:
            matched_halt_indices.add(h_idx)

    tp = len(matched_halt_indices)
    fp = n_fires - len(matched_fire_indices)
    fn = n_halts - tp

    recall = tp / n_halts if n_halts > 0 else 0.0
    precision = tp / n_fires if n_fires > 0 else 0.0
    fp_rate = fp / n_fires if n_fires > 0 else 0.0

    # --- Liquidity penalty (V3c T4 fix: normalized to [0, 1]) ---
    denom = target_spread_bps if target_spread_bps > 0 else 1.0
    liq_penalties: List[float] = []
    for f in fires:
        if f.mid_price > 0:
            shares_needed = position_value_usd / f.mid_price
            insufficient = f.bid_size_shares < shares_needed
        else:
            insufficient = True  # penalise if price unknown
        raw = f.spread_bps if insufficient else 0.0
        liq_penalties.append(min(1.0, max(0.0, raw / denom)))

    mean_liq = sum(liq_penalties) / len(liq_penalties) if liq_penalties else 0.0

    composite = w_recall * recall - w_fp * fp_rate - w_liq * mean_liq

    return EventScore(
        n_fires=n_fires,
        n_halts=n_halts,
        tp=tp,
        fp=fp,
        fn=fn,
        recall=recall,
        precision=precision,
        fp_rate=fp_rate,
        mean_liq_penalty=mean_liq,
        composite=composite,
    )


def aggregate_scores(scores: List[EventScore]) -> EventScore:
    """Aggregate per-event EventScore objects into a single pooled score.

    TP/FP/FN are summed; recall/precision/fp_rate/composite are recomputed
    from the pooled counts rather than averaged.
    """
    if not scores:
        return EventScore(
            n_fires=0, n_halts=0, tp=0, fp=0, fn=0,
            recall=0.0, precision=0.0, fp_rate=0.0,
            mean_liq_penalty=0.0, composite=0.0,
        )

    total_fires = sum(s.n_fires for s in scores)
    total_halts = sum(s.n_halts for s in scores)
    total_tp = sum(s.tp for s in scores)
    total_fp = sum(s.fp for s in scores)
    total_fn = sum(s.fn for s in scores)

    recall = total_tp / total_halts if total_halts > 0 else 0.0
    precision = total_tp / total_fires if total_fires > 0 else 0.0
    fp_rate = total_fp / total_fires if total_fires > 0 else 0.0

    # Weighted mean liquidity penalty by number of fires
    if total_fires > 0:
        mean_liq = sum(
            s.mean_liq_penalty * s.n_fires for s in scores if s.n_fires > 0
        ) / total_fires
    else:
        mean_liq = 0.0

    # Re-derive composite using default weights (scores already have per-event composites)
    composite = sum(s.composite for s in scores) / len(scores)

    return EventScore(
        n_fires=total_fires,
        n_halts=total_halts,
        tp=total_tp,
        fp=total_fp,
        fn=total_fn,
        recall=recall,
        precision=precision,
        fp_rate=fp_rate,
        mean_liq_penalty=mean_liq,
        composite=composite,
    )
