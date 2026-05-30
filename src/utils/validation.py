"""Small validation / numeric-safety helpers shared across model modules.

These keep the financial math from blowing up on the degenerate inputs that are
common in a stress model (zero debt service, zero revenue, missing fields).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np


def safe_div(numerator: float, denominator: float, *, default: float = float("nan")) -> float:
    """Divide, returning ``default`` (NaN by default) on zero/invalid denominator.

    Used for ratios like DSCR/ICR where a zero denominator (no debt service) is
    economically "infinite coverage" rather than an error; callers decide how to
    present that.
    """
    try:
        if denominator == 0 or denominator is None:
            return default
        result = numerator / denominator
        if math.isinf(result) or math.isnan(result):
            return default
        return result
    except (TypeError, ZeroDivisionError):
        return default


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def is_share(value: float, *, tol: float = 1e-6) -> bool:
    """True if value is a valid share in [0, 1] (with tolerance)."""
    return -tol <= value <= 1 + tol


def require_shares_sum_to_one(
    shares: dict[str, float], *, tol: float = 1e-3, label: str = "shares"
) -> None:
    total = sum(shares.values())
    if not math.isclose(total, 1.0, abs_tol=tol):
        raise ValueError(
            f"{label} must sum to 1.0 (got {total:.4f}); shares={shares}"
        )


def zscore(series: Any, *, ddof: int = 0) -> np.ndarray:
    """Z-score a 1-D array-like, NaN-safe (NaNs ignored in mean/std, kept in output)."""
    arr = np.asarray(series, dtype=float)
    mean = np.nanmean(arr)
    std = np.nanstd(arr, ddof=ddof)
    if std == 0 or np.isnan(std):
        return np.zeros_like(arr)
    return (arr - mean) / std


def latest_zscore(series: Any, *, ddof: int = 0) -> float:
    """Z-score of the most recent (last) observation vs the series history."""
    arr = np.asarray(series, dtype=float)
    arr = arr[~np.isnan(arr)]
    if arr.size < 2:
        return 0.0
    std = np.nanstd(arr, ddof=ddof)
    if std == 0:
        return 0.0
    return float((arr[-1] - np.nanmean(arr)) / std)


def bps_to_decimal(bps: float) -> float:
    """Convert basis points to a decimal rate (100 bps -> 0.01)."""
    return bps / 10_000.0


def pct_to_decimal(pct: float) -> float:
    """Convert percent to decimal (5.0 -> 0.05)."""
    return pct / 100.0
