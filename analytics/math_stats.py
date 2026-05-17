"""Pure statistical functions used by the streaming engine.

These take a numpy array of prices (or a derived series) and return a scalar.
No side effects, no module-level mutable state — safe to call from anywhere.
"""
from __future__ import annotations

import numpy as np


HURST_ENABLED = True
HURST_THRESHOLD = 0.55         # H above this -> persistent / trending
HURST_MIN_POINTS = 20          # minimum prices required for a reliable estimate


def compute_efficiency_ratio(prices: np.ndarray) -> float:
    """Kaufman Efficiency Ratio: |net change| / sum(|tick-to-tick change|).

    1.0 => perfect trend (every tick in the same direction);
    0.0 => pure chop (all the path-length is cancelling moves).
    """
    if prices.size < 2:
        return 0.0

    net_change = abs(float(prices[-1]) - float(prices[0]))
    volatility = float(np.abs(np.diff(prices)).sum())
    if volatility == 0.0:
        return 0.0
    return net_change / volatility


def compute_hurst(prices: np.ndarray) -> float:
    """Rescaled-Range (R/S) Hurst exponent estimate on a price series.

    Returns 0.5 when there isn't enough data — that's the random-walk midpoint,
    which is the safe non-claim. Values above ~0.55 are read as persistent
    / trending; below ~0.45 as mean-reverting.
    """
    if not HURST_ENABLED or prices.size < HURST_MIN_POINTS:
        return 0.5

    increments = np.diff(prices)
    if increments.size < HURST_MIN_POINTS:
        return 0.5

    candidate_windows = np.array((8, 16, 32, 64, 128, 256), dtype=np.int64)
    candidate_windows = candidate_windows[candidate_windows <= increments.size]
    if candidate_windows.size < 2:
        return 0.5

    rs_points: list[tuple[float, float]] = []
    for window in candidate_windows:
        segment_count = increments.size // int(window)
        if segment_count < 2:
            continue

        trimmed = increments[-segment_count * int(window):].reshape(segment_count, int(window))
        demeaned = trimmed - trimmed.mean(axis=1, keepdims=True)
        cumulative = np.cumsum(demeaned, axis=1)
        ranges = cumulative.max(axis=1) - cumulative.min(axis=1)
        stds = trimmed.std(axis=1)
        valid = stds > 0
        if not np.any(valid):
            continue

        rs = ranges[valid] / stds[valid]
        rs = rs[rs > 0]
        if rs.size == 0:
            continue

        rs_points.append((float(window), float(rs.mean())))

    if len(rs_points) < 2:
        return 0.5

    window_sizes = np.log(np.array([point[0] for point in rs_points], dtype=np.float64))
    rs_values = np.log(np.array([point[1] for point in rs_points], dtype=np.float64))
    hurst = float(np.polyfit(window_sizes, rs_values, 1)[0])
    return float(np.clip(hurst, 0.0, 1.0))
