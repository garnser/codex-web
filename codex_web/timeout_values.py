"""Shared interpretation of application-owned elapsed timeout values."""

from __future__ import annotations

import math


def normalize_timeout(value: float | int | None) -> float | None:
    """Return a positive duration, or None for an explicitly disabled timeout.

    Keep stored values intact at schema boundaries; normalize only when creating
    a timer. This does not interpret leases, absolute deadlines or poll intervals.
    """
    if value is None:
        return None
    seconds = float(value)
    if not math.isfinite(seconds):
        raise ValueError("timeout must be finite")
    if seconds in (0.0, -1.0):
        return None
    if seconds < 0:
        raise ValueError("timeout must be positive, 0 or -1")
    return seconds


def validate_timeout(value: float | int | None) -> float | int | None:
    """Validate a serialized duration without rewriting its disabled sentinel."""
    normalize_timeout(value)
    return value


def minimum_timeout(*values: float | int | None) -> float | None:
    """Intersect enabled elapsed ceilings; disabled ceilings add no limit."""
    enabled = [seconds for value in values
               if (seconds := normalize_timeout(value)) is not None]
    return min(enabled) if enabled else None
