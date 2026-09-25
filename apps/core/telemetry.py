"""PHI-safe observability: structured logs, traces, metrics and Sentry scrub.

ADR-014 contract: every telemetry egress passes through an allowlist
redactor. Log records emit only allowlisted fields; span attributes and
metric labels are filtered by their own allowlists; the Sentry scrubber
reuses the same vocabulary. Clinical content never leaves the process:
messages are emitted as templates (``record.msg`` without ``args``
interpolation), free-form values are pattern-redacted, and metric label
values are restricted to a safe token charset.

The ``/internal/metrics`` endpoint is sessionless: it authenticates with an
operator bearer token and a loopback/allowlisted-network restriction, never
with clinic sessions. It is registered in the tenant bypass list
(``apps.tenancy.middleware.BYPASS_PATHS``) per D-19.
"""

from __future__ import annotations

import contextvars
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from typing import TYPE_CHECKING, Final, cast
from urllib.parse import urlsplit

import redis
import sentry_sdk
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.http import HttpRequest, HttpResponse
from django.views.decorators.http import require_GET
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Link, Status

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from django.http import HttpResponseBase
    from opentelemetry.util.types import AttributeValue
    from sentry_sdk.types import Event as SentryEvent
    from sentry_sdk.types import Hint as SentryHint

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allowlists (ADR-014): the only vocabulary telemetry may carry.
# ---------------------------------------------------------------------------

LOG_FIELD_ALLOWLIST: Final = frozenset(
    {
        "request_id",
        "route",
        "method",
        "status",
        "duration_ms",
        "task",
        "queue",
        "operation_id",
        "reason_code",
        "attempt",
        "event",
    }
)
SPAN_ATTRIBUTE_ALLOWLIST: Final = frozenset(
    {
        "http.request.method",
        "http.route",
        "http.response.status_code",
        "clinic.request_id",
        "clinic.queue",
        "clinic.task",
        "clinic.operation_id",
        "clinic.reason_code",
    }
)
SENTRY_EVENT_ALLOWLIST: Final = frozenset(
    {
        "event_id",
        "timestamp",
        "level",
        "logger",
        "platform",
        "transaction",
        "release",
        "environment",
        "server_name",
        "sdk",
        "tags",
        "request",
        "exception",
        "breadcrumbs",
        "contexts",
    }
)
SENTRY_TAG_ALLOWLIST: Final = frozenset({"request_id", "route"})
SENTRY_REQUEST_ALLOWLIST: Final = frozenset({"method"})
SENTRY_BREADCRUMB_ALLOWLIST: Final = frozenset(
    {"type", "category", "level", "timestamp"}
)
SENTRY_CONTEXT_ALLOWLIST: Final = frozenset({"trace"})
# SDK-generated metadata keys that pass through verbatim; they are machine
# values (event ids, timestamps, versions), never caller-controlled text.
SENTRY_PASSTHROUGH_KEYS: Final = frozenset(
    {
        "event_id",
        "timestamp",
        "level",
        "logger",
        "platform",
        "release",
        "environment",
        "server_name",
        "sdk",
    }
)

# ---------------------------------------------------------------------------
# Value hygiene: pattern redaction + strict token charset.
# ---------------------------------------------------------------------------

REDACTED: Final = "[redacted]"
INVALID_LABEL: Final = "[invalid]"
MAX_FIELD_VALUE_LENGTH: Final = 128
MAX_LABEL_VALUE_LENGTH: Final = 64

_REDACTION_PATTERNS: Final = (
    # Test/egress sentinels must never survive a boundary.
    re.compile(r"SINTETICO-SENTINELA-[A-Za-z0-9_-]*", re.IGNORECASE),
    # Brazilian CPF shapes: 000.000.000-00 and bare 11 digits.
    re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b"),
    re.compile(r"(?<![\d.-])\d{11}(?![\d.-])"),
    # E-mail addresses.
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    # Bearer/authorization material.
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
)
# Strict token charset for values that cross an egress boundary: lowercase
# machine tokens only. Anything else (names, sentences, paths with query
# strings, uppercase text) collapses to a placeholder.
_SAFE_TOKEN: Final = re.compile(r"[a-z0-9_.:\-]{1,128}")
# Exception type names legitimately carry capitals (``ValueError``).
_SAFE_TYPE_NAME: Final = re.compile(r"[A-Za-z0-9_.]{1,128}")
_REQUEST_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_REQUEST_ID_HEADER: Final = "HTTP_X_REQUEST_ID"
_REQUEST_ID_RESPONSE_HEADER: Final = "X-Request-ID"


def redact_text(value: str) -> str:
    """Replace known PHI/token shapes inside one free-form string."""
    redacted = value
    for pattern in _REDACTION_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted


def _clean_field_value(value: object) -> str | int | float | bool:
    """Coerce one allowlisted field to a safe scalar or a placeholder."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    text = redact_text(str(value))[:MAX_FIELD_VALUE_LENGTH]
    if _SAFE_TOKEN.fullmatch(text):
        return text
    return INVALID_LABEL


def _safe_label(value: object) -> str:
    """Coerce one metric label value to the safe token charset."""
    text = redact_text(str(value))[:MAX_LABEL_VALUE_LENGTH]
    if _SAFE_TOKEN.fullmatch(text):
        return text
    return INVALID_LABEL


def _safe_type_name(value: object) -> str:
    """Coerce an exception type name; capitals are legitimate here."""
    text = redact_text(str(value))[:MAX_FIELD_VALUE_LENGTH]
    if _SAFE_TYPE_NAME.fullmatch(text):
        return text
    return INVALID_LABEL


# ---------------------------------------------------------------------------
# Request id propagation.
# ---------------------------------------------------------------------------

_current_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "clinic_request_id",
    default="",
)


def current_request_id() -> str:
    """Return the request id bound to this execution context, or ``""``."""
    return _current_request_id.get()


def _new_request_id() -> str:
    return uuid.uuid4().hex


def _inbound_request_id(request: HttpRequest) -> str:
    """Accept a well-formed inbound request id, else mint a fresh one."""
    candidate = request.META.get(_REQUEST_ID_HEADER, "")
    if isinstance(candidate, str) and _REQUEST_ID.fullmatch(candidate):
        return candidate
    return _new_request_id()


# ---------------------------------------------------------------------------
# Structured JSON logging.
# ---------------------------------------------------------------------------

_RESERVED_RECORD_ATTRS: Final = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
        "asctime",
    }
)


class AllowlistLogFilter(logging.Filter):
    """Drop every non-allowlisted ``extra`` attribute from a log record.

    The filter mutates the record so downstream handlers (including
    third-party formatters) cannot emit dropped fields either.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Strip non-allowlisted extras; the record always passes through."""
        for name in tuple(vars(record)):
            if name in _RESERVED_RECORD_ATTRS or name in LOG_FIELD_ALLOWLIST:
                continue
            delattr(record, name)
        return True


class JsonTelemetryFormatter(logging.Formatter):
    """Render one log record as a single allowlisted JSON object.

    The message is emitted as the literal template (``record.msg``) with
    pattern redaction; ``args`` are never interpolated, so a caller that
    passes PHI as a positional argument cannot leak it. Exception output is
    the exception type name only — never the message or traceback.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Serialize the record; non-allowlisted extras are already gone."""
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": redact_text(str(record.msg)),
        }
        for field in sorted(LOG_FIELD_ALLOWLIST):
            if field in vars(record):
                payload[field] = _clean_field_value(getattr(record, field))
        request_id = current_request_id()
        if request_id and "request_id" not in payload:
            payload["request_id"] = request_id
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception_type"] = _safe_type_name(record.exc_info[0].__name__)
        return json.dumps(payload, ensure_ascii=True, sort_keys=True)


# ---------------------------------------------------------------------------
# OpenTelemetry tracing (disabled by default).
# ---------------------------------------------------------------------------

_OTEL_ENABLED_ENV: Final = "CLINIC_OTEL_ENABLED"
_OTEL_EXPORTER_ENV: Final = "CLINIC_OTEL_EXPORTER"
_OTLP_ENDPOINT_ENV: Final = "CLINIC_OTEL_OTLP_ENDPOINT"
_OTLP_TRACES_PATH: Final = "/v1/traces"
_OTLP_TIMEOUT_SECONDS: Final = 5
_HTTP_SUCCESS_STATUSES: Final = range(200, 300)
_EXPORTER_CONSOLE: Final = "console"
_EXPORTER_OTLP: Final = "otlp-http-json"


def _filtered_attributes(
    attributes: Mapping[str, object] | None,
) -> dict[str, AttributeValue]:
    """Keep allowlisted span attributes; clean every string value."""
    if not attributes:
        return {}
    filtered: dict[str, AttributeValue] = {}
    for key, value in attributes.items():
        if key not in SPAN_ATTRIBUTE_ALLOWLIST:
            continue
        if isinstance(value, str):
            filtered[key] = _clean_field_value(value)
        elif isinstance(value, (list, tuple)):
            filtered[key] = cast(
                "AttributeValue",
                [
                    _clean_field_value(item) if isinstance(item, str) else item
                    for item in value
                ],
            )
        else:
            filtered[key] = cast("AttributeValue", value)
    return filtered


def _filtered_span(span: ReadableSpan) -> ReadableSpan:
    """Rebuild one span keeping only allowlisted attributes and safe names."""
    status = span.status
    description = redact_text(status.description or "")
    events = tuple(
        Event(
            name=redact_text(event.name),
            attributes=_filtered_attributes(event.attributes),
            timestamp=event.timestamp,
        )
        for event in span.events
    )
    links = tuple(
        Link(link.context, attributes=_filtered_attributes(link.attributes))
        for link in span.links
    )
    return ReadableSpan(
        name=redact_text(span.name),
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=_filtered_attributes(span.attributes),
        events=events,
        links=links,
        kind=span.kind,
        instrumentation_scope=span.instrumentation_scope,
        status=Status(status.status_code, description or None),
        start_time=span.start_time,
        end_time=span.end_time,
    )


class AllowlistSpanExporter(SpanExporter):
    """Wrap a span exporter so only allowlisted attributes ever leave."""

    def __init__(self, inner: SpanExporter) -> None:
        """Store the wrapped exporter that receives filtered spans."""
        self._inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Export filtered copies; the wrapped exporter sees no raw spans."""
        try:
            return self._inner.export([_filtered_span(span) for span in spans])
        except Exception:  # noqa: BLE001 - export must never break the app
            logger.warning("telemetry span export failed")
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        """Shut the wrapped exporter down."""
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Flush the wrapped exporter."""
        return self._inner.force_flush(timeout_millis)


class OtlpJsonSpanExporter(SpanExporter):
    """Minimal OTLP/HTTP JSON span exporter (no protobuf dependency).

    ``opentelemetry-sdk`` ships no OTLP exporter; this posts the standard
    OTLP JSON mapping to ``<endpoint>/v1/traces`` with a bounded timeout.
    """

    def __init__(self, endpoint: str) -> None:
        """Parse and validate the collector endpoint; fail closed on bad URL."""
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            message = "CLINIC_OTEL_OTLP_ENDPOINT must be an http(s) URL"
            raise ImproperlyConfigured(message)
        self._host = parsed.hostname
        self._port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._secure = parsed.scheme == "https"
        base = parsed.path.rstrip("/")
        self._path = f"{base}{_OTLP_TRACES_PATH}"

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """POST one OTLP JSON batch; any transport failure is FAILURE."""
        connection_class = HTTPSConnection if self._secure else HTTPConnection
        connection = connection_class(
            self._host,
            self._port,
            timeout=_OTLP_TIMEOUT_SECONDS,
        )
        try:
            connection.request(
                "POST",
                self._path,
                body=self._encode(spans),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            response.read()
        except (OSError, HTTPException):
            return SpanExportResult.FAILURE
        finally:
            connection.close()
        if response.status in _HTTP_SUCCESS_STATUSES:
            return SpanExportResult.SUCCESS
        return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        """No persistent resources to release."""

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002
        """Report success; the synchronous exporter buffers nothing."""
        return True

    @staticmethod
    def _encode(spans: Sequence[ReadableSpan]) -> bytes:
        return json.dumps(
            {
                "resourceSpans": [
                    {
                        "resource": {"attributes": []},
                        "scopeSpans": [
                            {
                                "scope": {"name": "clinic-os"},
                                "spans": [_otlp_span(span) for span in spans],
                            }
                        ],
                    }
                ]
            },
            ensure_ascii=True,
        ).encode()


def _otlp_span(span: ReadableSpan) -> dict[str, object]:
    context = span.context
    parent = span.parent
    return {
        "traceId": f"{context.trace_id:032x}" if context else "",
        "spanId": f"{context.span_id:016x}" if context else "",
        "parentSpanId": f"{parent.span_id:016x}" if parent else "",
        "name": span.name,
        "kind": int(span.kind.value) if span.kind else 1,
        "startTimeUnixNano": str(span.start_time or 0),
        "endTimeUnixNano": str(span.end_time or 0),
        "attributes": [
            {"key": key, "value": _otlp_value(value)}
            for key, value in (span.attributes or {}).items()
        ],
        "status": {"code": int(span.status.status_code.value)},
    }


def _otlp_value(value: object) -> dict[str, object]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def _build_exporter(environment: Mapping[str, str]) -> SpanExporter:
    kind = environment.get(_OTEL_EXPORTER_ENV, _EXPORTER_CONSOLE)
    if kind == _EXPORTER_CONSOLE:
        return AllowlistSpanExporter(ConsoleSpanExporter())
    if kind == _EXPORTER_OTLP:
        endpoint = environment.get(_OTLP_ENDPOINT_ENV, "")
        return AllowlistSpanExporter(OtlpJsonSpanExporter(endpoint))
    message = f"unsupported {_OTEL_EXPORTER_ENV}: {kind!r}"
    raise ImproperlyConfigured(message)


def build_tracer_provider(
    environment: Mapping[str, str],
) -> TracerProvider | None:
    """Build a configured ``TracerProvider``; ``None`` when disabled.

    Tracing is opt-in (``CLINIC_OTEL_ENABLED``). Exporter failures are
    contained by ``BatchSpanProcessor`` and ``AllowlistSpanExporter``, so a
    dead collector never affects the application.
    """
    if environment.get(_OTEL_ENABLED_ENV, "").lower() not in {"1", "true", "yes"}:
        return None
    provider = TracerProvider(resource=Resource.create({"service.name": "clinic-os"}))
    provider.add_span_processor(BatchSpanProcessor(_build_exporter(environment)))
    return provider


def configure_tracing(environment: Mapping[str, str]) -> None:
    """Install the global tracer provider when tracing is enabled."""
    provider = build_tracer_provider(environment)
    if provider is None:
        return
    trace.set_tracer_provider(provider)


# ---------------------------------------------------------------------------
# Metrics registry and collectors.
# ---------------------------------------------------------------------------

REQUEST_LATENCY_BUCKETS_SECONDS: Final = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)
_QUEUE_TIMEOUT_SECONDS: Final = 1.0


@dataclass(frozen=True, slots=True)
class _HistogramKey:
    route: str
    method: str
    status_class: str


class _Histogram:
    __slots__ = ("buckets", "count", "total")

    def __init__(self) -> None:
        self.buckets = [0] * (len(REQUEST_LATENCY_BUCKETS_SECONDS) + 1)
        self.count = 0
        self.total = 0.0

    def observe(self, value: float) -> None:
        # Prometheus buckets are cumulative: every bound >= value counts.
        self.count += 1
        self.total += value
        for index, bound in enumerate(REQUEST_LATENCY_BUCKETS_SECONDS):
            if value <= bound:
                self.buckets[index] += 1
        self.buckets[-1] += 1


class MetricsRegistry:
    """In-process SLI registry; every label value is sanitized on write."""

    def __init__(self) -> None:
        """Initialize empty metric families."""
        self._lock = threading.Lock()
        self._request_histograms: dict[_HistogramKey, _Histogram] = {}
        self._ai_invocations: dict[tuple[str, str], int] = {}
        self._ai_latency_sum: dict[tuple[str, str], float] = {}
        self._ai_cost_sum: dict[tuple[str, str], int] = {}
        self._scrape_errors: dict[str, int] = {}
        self._provider_probes: dict[str, Callable[[], bool]] = {}

    def observe_request(
        self, *, route: str, method: str, status: int, seconds: float
    ) -> None:
        """Record one request latency under route-name/method/status labels."""
        key = _HistogramKey(
            route=_safe_label(route),
            method=_safe_label(method.lower()),
            status_class=_safe_label(f"{status // 100}xx"),
        )
        with self._lock:
            histogram = self._request_histograms.setdefault(key, _Histogram())
            histogram.observe(seconds)

    def record_ai_invocation(
        self, *, capability: str, outcome: str, seconds: float, cost_micros: int
    ) -> None:
        """Aggregate one AI invocation by capability/outcome (todo 38 hook)."""
        key = (_safe_label(capability), _safe_label(outcome))
        with self._lock:
            self._ai_invocations[key] = self._ai_invocations.get(key, 0) + 1
            self._ai_latency_sum[key] = self._ai_latency_sum.get(key, 0.0) + seconds
            self._ai_cost_sum[key] = self._ai_cost_sum.get(key, 0) + cost_micros

    def register_provider_probe(
        self, capability: str, probe: Callable[[], bool]
    ) -> None:
        """Register a provider health probe keyed by capability (todo 4 hook)."""
        with self._lock:
            self._provider_probes[_safe_label(capability)] = probe

    def note_scrape_error(self, source: str) -> None:
        """Count one failed collector so silent gaps stay visible."""
        with self._lock:
            key = _safe_label(source)
            self._scrape_errors[key] = self._scrape_errors.get(key, 0) + 1

    def render(self) -> str:
        """Render every family plus fresh collector output as text format."""
        sections = [
            self._render_request_histograms(),
            self._render_ai_invocations(),
            self._render_scrape_errors(),
            render_queue_metrics(),
            render_outbox_metrics(),
            self._render_provider_health(),
        ]
        return "".join(sections)

    def _render_request_histograms(self) -> str:
        with self._lock:
            snapshot = [
                (
                    key,
                    list(histogram.buckets),
                    histogram.count,
                    histogram.total,
                )
                for key, histogram in sorted(
                    self._request_histograms.items(),
                    key=lambda item: (
                        item[0].route,
                        item[0].method,
                        item[0].status_class,
                    ),
                )
            ]
        lines = [
            "# HELP clinic_http_request_duration_seconds Request latency.",
            "# TYPE clinic_http_request_duration_seconds histogram",
        ]
        for key, buckets, count, total in snapshot:
            labels = (
                f'route="{key.route}",method="{key.method}",'
                f'status_class="{key.status_class}"'
            )
            for index, bound in enumerate(REQUEST_LATENCY_BUCKETS_SECONDS):
                lines.append(
                    "clinic_http_request_duration_seconds_bucket{"
                    f'{labels},le="{bound}"}} {buckets[index]}'
                )
            lines.append(
                "clinic_http_request_duration_seconds_bucket{"
                f'{labels},le="+Inf"}} {count}'
            )
            lines.append(
                f"clinic_http_request_duration_seconds_sum{{{labels}}} {total:.6f}"
            )
            lines.append(
                f"clinic_http_request_duration_seconds_count{{{labels}}} {count}"
            )
        return "\n".join(lines) + "\n"

    def _render_ai_invocations(self) -> str:
        with self._lock:
            invocations = dict(self._ai_invocations)
            latency = dict(self._ai_latency_sum)
            cost = dict(self._ai_cost_sum)
        lines = [
            "# HELP clinic_ai_invocations_total AI invocations.",
            "# TYPE clinic_ai_invocations_total counter",
        ]
        for (capability, outcome), count in sorted(invocations.items()):
            labels = f'capability="{capability}",outcome="{outcome}"'
            lines.append(f"clinic_ai_invocations_total{{{labels}}} {count}")
            lines.append(
                "clinic_ai_invocation_latency_seconds_sum"
                f"{{{labels}}} {latency.get((capability, outcome), 0.0):.6f}"
            )
            lines.append(
                "clinic_ai_invocation_cost_micros_sum"
                f"{{{labels}}} {cost.get((capability, outcome), 0)}"
            )
        return "\n".join(lines) + "\n"

    def _render_scrape_errors(self) -> str:
        with self._lock:
            errors = dict(self._scrape_errors)
        lines = [
            "# HELP clinic_metrics_scrape_errors_total Failed metric collectors.",
            "# TYPE clinic_metrics_scrape_errors_total counter",
        ]
        for source, count in sorted(errors.items()):
            lines.append(
                f'clinic_metrics_scrape_errors_total{{source="{source}"}} {count}'
            )
        return "\n".join(lines) + "\n"

    def _render_provider_health(self) -> str:
        lines = [
            "# HELP clinic_provider_health Provider health by capability (1=healthy).",
            "# TYPE clinic_provider_health gauge",
        ]
        with self._lock:
            probes = dict(self._provider_probes)
        for capability, probe in sorted(probes.items()):
            try:
                healthy = bool(probe())
            except Exception:  # noqa: BLE001 - a crashing probe is unhealthy
                healthy = False
            lines.append(
                f'clinic_provider_health{{capability="{capability}"}} '
                f"{1 if healthy else 0}"
            )
        return "\n".join(lines) + "\n"


METRICS: Final = MetricsRegistry()


def _queue_names() -> list[str]:
    """Resolve configured Celery queue names from the celery app config."""
    names: set[str] = set()
    try:
        from config.celery import app as celery_app  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - celery config must not break a scrape
        METRICS.note_scrape_error("queue-config")
        return []
    conf = celery_app.conf
    default = conf.get("task_default_queue")
    if isinstance(default, str) and default:
        names.add(default)
    routes = conf.get("task_routes") or {}
    if isinstance(routes, dict):
        for route in routes.values():
            queue = route.get("queue") if isinstance(route, dict) else route
            name = getattr(queue, "name", queue)
            if isinstance(name, str) and name:
                names.add(name)
    queues = conf.get("task_queues") or {}
    for queue in queues.values() if isinstance(queues, dict) else queues:
        name = getattr(queue, "name", None)
        if isinstance(name, str) and name:
            names.add(name)
    return sorted(names)


def _message_enqueued_at(raw: object) -> float | None:
    """Extract an enqueue timestamp from one broker message, if present."""
    if not isinstance(raw, (bytes, str)):
        return None
    try:
        message = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(message, dict):
        return None
    for section in ("headers", "properties"):
        candidate = message.get(section)
        if isinstance(candidate, dict):
            stamp = candidate.get("timestamp")
            if (
                isinstance(stamp, (int, float))
                and not isinstance(stamp, bool)
                and stamp > 0
            ):
                return float(stamp)
    return None


def render_queue_metrics() -> str:
    """Scrape broker queue depth and best-effort oldest-message age.

    Depth comes from ``LLEN``. Age is emitted only when the oldest message
    carries a ``timestamp`` header/property; the Redis transport does not
    stamp one, so the series is absent rather than fabricated. Durable
    backlog age is covered by ``clinic_outbox_oldest_age_seconds``.
    """
    lines = [
        "# HELP clinic_queue_depth Messages waiting per Celery queue.",
        "# TYPE clinic_queue_depth gauge",
        "# HELP clinic_queue_oldest_age_seconds Age of the oldest queued message.",
        "# TYPE clinic_queue_oldest_age_seconds gauge",
    ]
    broker_url = getattr(settings, "CELERY_BROKER_URL", "")
    if not broker_url.startswith("redis"):
        return "\n".join(lines) + "\n"
    try:
        client = redis.Redis.from_url(
            broker_url,
            socket_connect_timeout=_QUEUE_TIMEOUT_SECONDS,
            socket_timeout=_QUEUE_TIMEOUT_SECONDS,
        )
        try:
            for queue in _queue_names():
                depth = client.llen(queue)
                label = _safe_label(queue)
                lines.append(f'clinic_queue_depth{{queue="{label}"}} {depth}')
                if depth:
                    enqueued = _message_enqueued_at(client.lindex(queue, -1))
                    if enqueued is not None:
                        age = max(0.0, time.time() - enqueued)
                        lines.append(
                            "clinic_queue_oldest_age_seconds"
                            f'{{queue="{label}"}} {age:.3f}'
                        )
        finally:
            client.close()
    except Exception:  # noqa: BLE001 - broker down must not break the scrape
        METRICS.note_scrape_error("queue")
    return "\n".join(lines) + "\n"


def render_outbox_metrics() -> str:
    """Scrape outbox operation counts and oldest age per status.

    Reads through the ``comms_operation_state_counts_v1`` SECURITY DEFINER
    resolver so the sessionless scrape works as ``clinic_app`` without any
    tenant GUC and without direct table grants.
    """
    lines = [
        "# HELP clinic_outbox_operations Outbox operations by status.",
        "# TYPE clinic_outbox_operations gauge",
        "# HELP clinic_outbox_oldest_age_seconds Oldest outbox age by status.",
        "# TYPE clinic_outbox_oldest_age_seconds gauge",
    ]
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, operation_count, oldest_created_at "
                "FROM clinic_app.comms_operation_state_counts_v1()"
            )
            rows = cursor.fetchall()
    except Exception:  # noqa: BLE001 - a dead DB must not break the scrape
        METRICS.note_scrape_error("outbox")
        return "\n".join(lines) + "\n"
    now = datetime.now(tz=UTC)
    for status, count, oldest in rows:
        label = _safe_label(status)
        lines.append(f'clinic_outbox_operations{{status="{label}"}} {int(count)}')
        if isinstance(oldest, datetime):
            age = max(0.0, (now - oldest).total_seconds())
            lines.append(
                f'clinic_outbox_oldest_age_seconds{{status="{label}"}} {age:.3f}'
            )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Request middleware: request id, latency SLI, request span, access log.
# ---------------------------------------------------------------------------

_UNNAMED_ROUTE: Final = "unmatched"


def _route_name(request: HttpRequest) -> str:
    match = getattr(request, "resolver_match", None)
    if match is None or not match.url_name:
        return _UNNAMED_ROUTE
    return str(match.url_name)


class TelemetryMiddleware:
    """Bind a request id, record the latency SLI and emit one access log."""

    def __init__(
        self,
        get_response: Callable[[HttpRequest], HttpResponseBase],
    ) -> None:
        """Store the downstream handler."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        """Wrap the request in telemetry context; never blocks the response."""
        request_id = _inbound_request_id(request)
        token = _current_request_id.set(request_id)
        sentry_sdk.get_current_scope().set_tag("request_id", request_id)
        started = time.monotonic()
        status = 500
        try:
            tracer = trace.get_tracer("apps.core.telemetry")
            with tracer.start_as_current_span("http.request") as span:
                span.set_attribute(
                    "http.request.method",
                    (request.method or "get").lower(),
                )
                span.set_attribute("clinic.request_id", request_id)
                response = self.get_response(request)
                status = response.status_code
                span.set_attribute("http.route", _route_name(request))
                span.set_attribute("http.response.status_code", status)
                response.headers[_REQUEST_ID_RESPONSE_HEADER] = request_id
                return response
        finally:
            elapsed = time.monotonic() - started
            route = _route_name(request)
            METRICS.observe_request(
                route=route,
                method=request.method or "get",
                status=status,
                seconds=elapsed,
            )
            logger.info(
                "http request",
                extra={
                    "request_id": request_id,
                    "route": route,
                    "method": (request.method or "get").lower(),
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 3),
                },
            )
            _current_request_id.reset(token)


# ---------------------------------------------------------------------------
# /internal/metrics endpoint (ops token + network restriction, D-19).
# ---------------------------------------------------------------------------

# This constant holds an environment variable NAME, not a secret value.
OPS_METRICS_TOKEN_ENV: Final = "CLINIC_OPS_METRICS_TOKEN"  # noqa: S105
OPS_METRICS_NETWORKS_ENV: Final = "CLINIC_OPS_METRICS_ALLOWED_NETWORKS"
_DEFAULT_METRICS_NETWORKS: Final = ("127.0.0.0/8", "::1/128")
_MIN_TOKEN_LENGTH: Final = 16
_METRICS_CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"


def _allowed_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    raw = os.environ.get(OPS_METRICS_NETWORKS_ENV, "")
    networks = raw.split(",") if raw.strip() else list(_DEFAULT_METRICS_NETWORKS)
    parsed: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for network in networks:
        try:
            parsed.append(ipaddress.ip_network(network.strip(), strict=False))
        except ValueError:
            continue
    return tuple(parsed)


def _client_ip_allowed(request: HttpRequest) -> bool:
    """Check the direct peer address; forwarded headers are never trusted."""
    remote = request.META.get("REMOTE_ADDR", "")
    try:
        address = ipaddress.ip_address(remote)
    except ValueError:
        return False
    networks = _allowed_networks()
    return any(address in network for network in networks)


def _ops_token_valid(request: HttpRequest) -> bool:
    """Require a configured bearer token; fail closed when unset/short."""
    expected = os.environ.get(OPS_METRICS_TOKEN_ENV, "")
    if len(expected) < _MIN_TOKEN_LENGTH:
        return False
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if not isinstance(header, str) or not header.startswith("Bearer "):
        return False
    return secrets.compare_digest(header[len("Bearer ") :], expected)


@require_GET
def internal_metrics(request: HttpRequest) -> HttpResponse:
    """Serve the Prometheus text exposition for operators only.

    Network restriction runs first so the endpoint is indistinguishable
    from a missing route off the allowlist; a bad or missing token then
    gets a bare 401. No tenant context is opened and no clinical content
    is ever rendered.
    """
    if not _client_ip_allowed(request):
        return HttpResponse(status=404)
    if not _ops_token_valid(request):
        return HttpResponse(status=401)
    return HttpResponse(METRICS.render(), content_type=_METRICS_CONTENT_TYPE)


# ---------------------------------------------------------------------------
# Sentry scrubber (reuses the allowlist vocabulary).
# ---------------------------------------------------------------------------


def _scrub_exception(exception: object) -> dict[str, object]:
    if not isinstance(exception, dict):
        return {}
    values = exception.get("values")
    if not isinstance(values, list):
        return {}
    scrubbed = []
    for entry in values:
        if not isinstance(entry, dict):
            continue
        kept: dict[str, object] = {}
        if "type" in entry:
            kept["type"] = _safe_type_name(entry["type"])
        mechanism = entry.get("mechanism")
        if isinstance(mechanism, dict) and isinstance(mechanism.get("type"), str):
            kept["mechanism"] = {"type": _safe_type_name(mechanism["type"])}
        scrubbed.append(kept)
    return {"values": scrubbed}


def _scrub_breadcrumbs(breadcrumbs: object) -> dict[str, object]:
    entries = breadcrumbs.get("values") if isinstance(breadcrumbs, dict) else None
    if not isinstance(entries, list):
        return {}
    scrubbed = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        scrubbed.append(
            {
                key: _clean_field_value(value)
                for key, value in entry.items()
                if key in SENTRY_BREADCRUMB_ALLOWLIST
            }
        )
    return {"values": scrubbed}


def scrub_sentry_event(event: SentryEvent, _hint: SentryHint) -> SentryEvent:
    """Reduce one Sentry event to allowlisted, PHI-free fields.

    The surviving vocabulary is event metadata, the exception *type* (never
    the message), the request *method* (never URL/headers/body), allowlisted
    tags such as ``request_id`` and structural breadcrumbs. Everything else
    is dropped before the event leaves the process.
    """
    scrubbed: dict[str, object] = {}
    for key, value in dict(event).items():
        if key not in SENTRY_EVENT_ALLOWLIST:
            continue
        if key in SENTRY_PASSTHROUGH_KEYS:
            scrubbed[key] = value
        elif key == "request" and isinstance(value, dict):
            scrubbed[key] = {
                sub: _clean_field_value(
                    sub_value.lower() if isinstance(sub_value, str) else sub_value
                )
                for sub, sub_value in value.items()
                if sub in SENTRY_REQUEST_ALLOWLIST
            }
        elif key == "tags" and isinstance(value, dict):
            scrubbed[key] = {
                sub: _clean_field_value(sub_value)
                for sub, sub_value in value.items()
                if sub in SENTRY_TAG_ALLOWLIST
            }
        elif key == "exception":
            scrubbed[key] = _scrub_exception(value)
        elif key == "breadcrumbs":
            scrubbed[key] = _scrub_breadcrumbs(value)
        elif key == "contexts" and isinstance(value, dict):
            scrubbed[key] = {
                sub: sub_value
                for sub, sub_value in value.items()
                if sub in SENTRY_CONTEXT_ALLOWLIST
            }
        elif isinstance(value, str):
            scrubbed[key] = _clean_field_value(value)
        else:
            scrubbed[key] = value
    return cast("SentryEvent", scrubbed)
