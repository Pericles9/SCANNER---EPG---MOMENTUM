"""Volume acceleration for EXIT 3 still-in-play confirmation.

V_ddot(t) = VolRate(t, W) - VolRate(t - W, 2W)

where W = window_sec (default 5s).
  - Current window:  shares in (t - W, t]  /  W
  - Baseline window: shares in (t - 3W, t - W]  /  2W

Returns NaN (no-signal, not a veto) if fewer than min_trades
in the current window.
"""

import numpy as np
import numba as nb


@nb.njit(cache=True)
def _vol_accel_at(timestamps_ns, sizes, t_ns, window_ns,
                  baseline_window_ns, min_trades):
    """Compute volume acceleration at a single point in time.

    Parameters
    ----------
    timestamps_ns : int64 array, sorted ascending
    sizes : float64 array, trade sizes in shares
    t_ns : int64, evaluation time in nanoseconds
    window_ns : int64, current window width in nanoseconds
    baseline_window_ns : int64, baseline window width in nanoseconds
    min_trades : int, minimum trades in current window

    Returns
    -------
    float64 : shares/sec acceleration, or NaN
    """
    current_start = t_ns - window_ns
    baseline_start = t_ns - window_ns - baseline_window_ns
    baseline_end = t_ns - window_ns

    current_vol = 0.0
    current_count = 0
    baseline_vol = 0.0

    for i in range(len(timestamps_ns)):
        ts = timestamps_ns[i]
        if ts > t_ns:
            break
        sz = sizes[i]
        if ts > current_start:
            current_vol += sz
            current_count += 1
        elif ts > baseline_start and ts <= baseline_end:
            baseline_vol += sz

    if current_count < min_trades:
        return np.nan

    window_sec = window_ns / 1_000_000_000.0
    baseline_sec = baseline_window_ns / 1_000_000_000.0

    current_rate = current_vol / window_sec
    baseline_rate = baseline_vol / baseline_sec

    return current_rate - baseline_rate


@nb.njit(cache=True)
def _vol_accel_series(timestamps_ns, sizes, window_ns,
                      baseline_window_ns, min_trades):
    """Compute volume acceleration at every trade timestamp.

    Scans backwards from each trade for efficiency (timestamps sorted).

    Returns
    -------
    float64 array of length N, NaN where insufficient data.
    """
    n = len(timestamps_ns)
    result = np.empty(n, dtype=np.float64)

    for j in range(n):
        t_ns = timestamps_ns[j]
        current_start = t_ns - window_ns
        baseline_start = t_ns - window_ns - baseline_window_ns
        baseline_end = t_ns - window_ns

        current_vol = 0.0
        current_count = 0
        baseline_vol = 0.0

        # Scan backwards from j — break early once past baseline window
        for i in range(j, -1, -1):
            ts = timestamps_ns[i]
            if ts <= baseline_start:
                break
            sz = sizes[i]
            if ts > current_start:
                # In current window (t_ns >= ts by construction since i <= j)
                current_vol += sz
                current_count += 1
            elif ts > baseline_start and ts <= baseline_end:
                baseline_vol += sz

        if current_count < min_trades:
            result[j] = np.nan
        else:
            window_sec = window_ns / 1_000_000_000.0
            baseline_sec = baseline_window_ns / 1_000_000_000.0
            current_rate = current_vol / window_sec
            baseline_rate = baseline_vol / baseline_sec
            result[j] = current_rate - baseline_rate

    return result


def compute_vol_accel(timestamps, sizes, window_sec=5, min_trades=10):
    """Compute volume acceleration at the current time (last timestamp).

    V_ddot(t) = VolRate(t, W) - VolRate(t - W, 2W)

    Parameters
    ----------
    timestamps : int64 array
        Trade timestamps in nanoseconds, sorted ascending.
    sizes : array-like
        Trade sizes in shares.
    window_sec : float
        Current window width in seconds (default 5).
    min_trades : int
        Minimum trades in current window; returns NaN if not met.
        NaN means no-signal, NOT a veto.

    Returns
    -------
    float
        Volume acceleration in shares/sec, or NaN.
    """
    if len(timestamps) == 0:
        return np.nan

    ts = np.asarray(timestamps, dtype=np.int64)
    sz = np.asarray(sizes, dtype=np.float64)

    t_ns = ts[-1]
    window_ns = np.int64(window_sec * 1_000_000_000)
    baseline_window_ns = np.int64(window_sec * 2 * 1_000_000_000)

    return _vol_accel_at(ts, sz, t_ns, window_ns, baseline_window_ns,
                         min_trades)


def compute_vol_accel_series(timestamps, sizes, window_sec=5, min_trades=10):
    """Compute volume acceleration at every trade timestamp.

    Useful for calibration and visualization — returns the full time
    series of V_ddot values.

    Parameters
    ----------
    timestamps : int64 array
        Trade timestamps in nanoseconds, sorted ascending.
    sizes : array-like
        Trade sizes in shares.
    window_sec : float
        Current window width in seconds (default 5).
    min_trades : int
        Minimum trades in current window; NaN where not met.

    Returns
    -------
    np.ndarray
        Float64 array of length N with vol_accel at each trade.
    """
    if len(timestamps) == 0:
        return np.array([], dtype=np.float64)

    ts = np.asarray(timestamps, dtype=np.int64)
    sz = np.asarray(sizes, dtype=np.float64)

    window_ns = np.int64(window_sec * 1_000_000_000)
    baseline_window_ns = np.int64(window_sec * 2 * 1_000_000_000)

    return _vol_accel_series(ts, sz, window_ns, baseline_window_ns,
                             min_trades)
