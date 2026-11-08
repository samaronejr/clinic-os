"""Strict descriptors, exact decimal arithmetic and payload-free errors."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apps.analytics.catalog import METRICS

if TYPE_CHECKING:
    from uuid import UUID

SCHEMA: Final = "analytics-query-v1"
MAX_QUERY_BYTES: Final = 4096
MAX_WINDOW: Final = timedelta(days=366)
WEEKDAYS: Final = 7
STAMP: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:[.][0-9]{1,6})?(?:Z|[+-](?:0[0-9]|1[0-4]):[0-5][0-9])"
)


class AnalyticsInputError(ValueError):
    """Do not reflect model output, questions or selectors in exceptions."""

    def __init__(self) -> None:
        """Expose a fixed validation error."""
        super().__init__("invalid metric query")


class AnalyticsAccessDeniedError(PermissionError):
    """Unknown and foreign objects have the same refusal."""

    def __init__(self) -> None:
        """Keep refusal details out of telemetry."""
        super().__init__("metric access denied")


class AnalyticsConflictError(ValueError):
    """Retained review and refresh commands cannot change their meaning."""

    def __init__(self) -> None:
        """Expose no stored or supplied payload."""
        super().__init__("metric command conflicts")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    data = dict(pairs)
    if len(data) != len(pairs):
        raise AnalyticsInputError
    return data


def decode(raw: str) -> object:
    """Reject duplicate keys, oversized input and nonstandard JSON constants."""
    if type(raw) is not str or len(raw.encode()) > MAX_QUERY_BYTES:
        raise AnalyticsInputError
    try:
        return json.loads(raw, object_pairs_hook=_pairs)
    except (ValueError, RecursionError) as error:
        raise AnalyticsInputError from error


@dataclass(frozen=True, slots=True)
class Query:
    """Only code selects a source view; the model selects these bounded values."""

    metric_id: str
    start: datetime
    end: datetime
    tz: str
    dimensions: tuple[str, ...] = ()
    weekdays: tuple[int, ...] = ()

    def payload(self) -> dict[str, object]:
        """Return the exact six-field model response schema."""
        return {
            "metric_id": self.metric_id,
            "version": 1,
            "dimensions": list(self.dimensions),
            "filters": {"weekdays": list(self.weekdays)} if self.weekdays else {},
            "window": {"start": self.start.isoformat(), "end": self.end.isoformat()},
            "tz": self.tz,
        }

    def encode(self) -> str:
        """Canonical local serialization for immutable commands."""
        return json.dumps(self.payload(), sort_keys=True, separators=(",", ":"))

    @property
    def digest(self) -> str:
        """Bind exact metric version, dimensions, filters, window and timezone."""
        return hashlib.sha256(self.encode().encode()).hexdigest()


def parse_descriptor(raw: str) -> Query:
    """Compile a model descriptor to typed parameters, never query text."""
    value = decode(raw)
    if not isinstance(value, dict) or set(value) != {
        "metric_id",
        "version",
        "dimensions",
        "filters",
        "window",
        "tz",
    }:
        raise AnalyticsInputError
    key = value["metric_id"]
    window = value["window"]
    filters = value["filters"]
    if (
        type(key) is not str
        or key not in METRICS
        or type(value["version"]) is not int
        or value["version"] != 1
        or value["dimensions"] not in ([], ["day"])
        or not isinstance(filters, dict)
        or set(filters) - {"weekdays"}
        or not isinstance(window, dict)
        or set(window) != {"start", "end"}
        or type(value["tz"]) is not str
    ):
        raise AnalyticsInputError
    days = filters.get("weekdays", [])
    if (
        not isinstance(days, list)
        or len(days) > WEEKDAYS
        or any(type(day) is not int or not 0 <= day < WEEKDAYS for day in days)
        or len(set(days)) != len(days)
        or (METRICS[key].time_basis == "as_of" and (days or value["dimensions"]))
        or (
            key in {"utilization", "activation", "retained_usage"}
            and (days or value["dimensions"])
        )
    ):
        raise AnalyticsInputError
    if not all(
        isinstance(window[item], str) and STAMP.fullmatch(window[item])
        for item in ("start", "end")
    ):
        raise AnalyticsInputError
    try:
        start = datetime.fromisoformat(window["start"])
        end = datetime.fromisoformat(window["end"])
        ZoneInfo(value["tz"])
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or not timedelta() < end - start <= MAX_WINDOW
        ):
            raise AnalyticsInputError
    except (ValueError, TypeError, ZoneInfoNotFoundError) as error:
        raise AnalyticsInputError from error
    return Query(
        key, start, end, value["tz"], tuple(value["dimensions"]), tuple(sorted(days))
    )


def descriptor(
    *, metric_id: str, start: datetime, end: datetime, tz: str
) -> dict[str, object]:
    """Build the same validated descriptor for the complete manual path."""
    return parse_descriptor(Query(metric_id, start, end, tz).encode()).payload()


def render_value(metric_id: str, numerator: str, denominator: str) -> str | None:
    """Money never passes through binary floats or model arithmetic."""
    metric = METRICS[metric_id]
    if not metric.available:
        return None
    top, bottom = Decimal(numerator), Decimal(denominator)
    if metric.unit in {"percent", "seconds"}:
        if bottom == 0:
            return None
        scale = 100 if metric.unit == "percent" else 1
        return str((top * scale / bottom).quantize(Decimal("0.01")))
    return format(top, "f")


@dataclass(frozen=True, slots=True)
class Result:
    """A read model retains the permission version and observed data time."""

    refresh_id: UUID
    query: Query
    authorization_version: str
    as_of: datetime
    rows: tuple[dict[str, str | None], ...]
    status: str
