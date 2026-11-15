"""Latency statistics with nearest-rank percentiles."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


class StatsInputError(ValueError):
    """Reject an empty or non-finite sample set."""


def percentile(samples: Sequence[float], fraction: float) -> float:
    """Return the nearest-rank percentile; ``fraction`` is within (0, 1]."""
    if not samples or not 0 < fraction <= 1:
        message = "percentile needs samples and a fraction in (0, 1]"
        raise StatsInputError(message)
    ordered = sorted(samples)
    if not all(math.isfinite(value) and value >= 0 for value in ordered):
        message = "samples must be finite and non-negative"
        raise StatsInputError(message)
    rank = math.ceil(fraction * len(ordered))
    return ordered[rank - 1]


@dataclass(frozen=True, slots=True)
class Summary:
    """Report p50/p95/p99 and the extremes of one sample set."""

    count: int
    p50: float
    p95: float
    p99: float
    maximum: float


def summarize(samples: Sequence[float]) -> Summary:
    """Summarize latencies in the unit the caller recorded them in."""
    return Summary(
        count=len(samples),
        p50=percentile(samples, 0.50),
        p95=percentile(samples, 0.95),
        p99=percentile(samples, 0.99),
        maximum=max(samples),
    )
