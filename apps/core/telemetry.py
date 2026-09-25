"""PHI-safe observability: structured logs, traces, metrics and Sentry scrub.

ADR-014 contract: every telemetry egress passes through a closed-vocabulary
allowlist, not a charset filter. Log messages must be registered templates
(args are never interpolated); log/span/metric values are validated per field
against closed sets (route names, queue names, capability keys, HTTP methods,
status classes) or strict machine formats (32-hex request ids, UUIDs); the
Sentry scrubber rebuilds events recursively from the same vocabulary.
Clinical content — names, SOAP text, identifiers, URLs, bodies — cannot
leave the process through any of these channels.

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
# Closed vocabularies (ADR-014): the only values telemetry may carry.
# ---------------------------------------------------------------------------

# Log messages are a closed set of registered templates. Anything else —
# including a literal string carrying clinical text — renders as [unlisted].
LOG_MESSAGE_ALLOWLIST: Final = frozenset(
    {
        "http request",
        "telemetry span export failed",
        "prescription signature dispatch failed",
        "billing action failed; rolled back",
        "retention action failed; rolled back",
        "patient records export failed; rolled back",
        "ehr draft save failed; transaction rolled back",
        "ehr history save failed; transaction rolled back",
        "ehr attachment upload failed; transaction rolled back",
        "ehr attachment storage failed",
        "teleconsult note save failed; transaction rolled back",
        "prescription save failed; transaction rolled back",
        "prescription document render failed",
        "prescription document storage failed",
        "prescription signed document storage failed",
        "patient document download failed",
        # Django's own request lifecycle warnings.
        "Unauthorized: %s",
        "Forbidden: %s",
        "Bad Request: %s",
        "Not Found: %s",
        "Method Not Allowed (%s): %s",
        "SuspiciousOperation at %s",
    }
)

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
SENTRY_TRACE_CONTEXT_ALLOWLIST: Final = frozenset(
    {"trace_id", "span_id", "parent_span_id", "op"}
)
SENTRY_LEVELS: Final = frozenset({"fatal", "error", "warning", "info", "debug"})
HTTP_METHODS: Final = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
)
AI_INVOCATION_OUTCOMES: Final = frozenset(
    {"success", "error", "denied", "deferred", "timeout"}
)
SCRAPE_ERROR_SOURCES: Final = frozenset({"queue", "queue-config", "outbox"})
OUTBOX_STATUSES: Final = frozenset(
    {"pending", "in_progress", "succeeded", "delivered", "failed", "cancelled"}
)
_SPAN_INTERNAL_NAMES: Final = frozenset({"http.request"})
_SPAN_EVENT_NAMES: Final = frozenset({"exception"})

# ---------------------------------------------------------------------------
# Value hygiene: strict formats and closed-set validators.
# ---------------------------------------------------------------------------

REDACTED: Final = "[redacted]"
INVALID_LABEL: Final = "[invalid]"
UNLISTED_MESSAGE: Final = "[unlisted]"
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
_SAFE_TOKEN: Final = re.compile(r"[a-z0-9_.:\-]{1,128}")
_SAFE_TYPE_NAME: Final = re.compile(r"[A-Za-z0-9_.]{1,128}")
# Request ids are exactly the minted format: 32 lowercase hex chars.
_REQUEST_ID_FORMAT: Final = re.compile(r"[0-9a-f]{32}")
_UUID_FORMAT: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_SHA_FORMAT: Final = re.compile(r"[0-9a-f]{40}")
_ISO_TIMESTAMP: Final = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
# Multi-segment hyphenated slugs are how synthetic person names arrive; a
# legitimate machine token never needs three hyphenated segments.
_SLUG_NAME: Final = re.compile(r"[a-z]+(?:-[a-z0-9]+){2,}")
_REQUEST_ID_HEADER: Final = "HTTP_X_REQUEST_ID"
_REQUEST_ID_RESPONSE_HEADER: Final = "X-Request-ID"


def redact_text(value: str) -> str:
    """Replace known PHI/token shapes inside one free-form string."""
    redacted = value
    for pattern in _REDACTION_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted


def _looks_sensitive(text: str) -> bool:
    """Report whether a candidate value matches a known-unsafe shape."""
    return (
        redact_text(text) != text
        or _UUID_FORMAT.fullmatch(text) is not None
        or _REQUEST_ID_FORMAT.fullmatch(text) is not None
        or _SHA_FORMAT.fullmatch(text) is not None
        or _SLUG_NAME.fullmatch(text) is not None
    )


def _machine_token(value: object) -> str:
    """Validate an open-vocabulary machine token; unsafe shapes collapse."""
    if not isinstance(value, str):
        return INVALID_LABEL
    text = value.strip().lower()[:MAX_FIELD_VALUE_LENGTH]
    if _SAFE_TOKEN.fullmatch(text) is None or _looks_sensitive(text):
        return INVALID_LABEL
    return text


def _hex32(value: object) -> str:
    """Validate a 32-hex request/trace id; anything else collapses."""
    if isinstance(value, str) and _REQUEST_ID_FORMAT.fullmatch(value.lower()):
        return value.lower()
    return INVALID_LABEL


def _uuid_value(value: object) -> str:
    """Validate a UUID-shaped identifier (dashed or bare hex)."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str):
        text = value.lower()
        if _UUID_FORMAT.fullmatch(text) or _REQUEST_ID_FORMAT.fullmatch(text):
            return text
    return INVALID_LABEL


def _int_range(value: object, low: int, high: int) -> int | str:
    """Validate a bounded integer field."""
    if isinstance(value, bool) or not isinstance(value, int):
        return INVALID_LABEL
    if low <= value <= high:
        return value
    return INVALID_LABEL


def _number_field(value: object) -> int | float | str:
    """Validate a numeric field; digit strings and PHI shapes collapse."""
    if isinstance(value, bool):
        return INVALID_LABEL
    if isinstance(value, (int, float)):
        return value
    return INVALID_LABEL


def _method_value(value: object) -> str:
    """Validate an HTTP method against the closed verb set."""
    if isinstance(value, str) and value.lower() in HTTP_METHODS:
        return value.lower()
    return INVALID_LABEL


_STATUS_CLASS_FORMAT: Final = re.compile(r"[1-5]xx")


def _status_class_value(value: object) -> str:
    """Validate a Prometheus status-class label (``2xx`` shape)."""
    if isinstance(value, str) and _STATUS_CLASS_FORMAT.fullmatch(value):
        return value
    return INVALID_LABEL


def _route_names() -> frozenset[str]:
    """Return the closed set of registered URL route names."""
    from django.urls import get_resolver  # noqa: PLC0415

    try:
        resolver = get_resolver()
        names = {name for name in resolver.reverse_dict if isinstance(name, str)}
    except Exception:  # noqa: BLE001 - resolver may be unavailable pre-setup
        return frozenset()
    return frozenset(names)


def _route_name_value(value: object) -> str:
    """Validate a route label against registered URL names."""
    if isinstance(value, str) and value in _route_names():
        return value
    if value == _UNNAMED_ROUTE:
        return _UNNAMED_ROUTE
    return INVALID_LABEL


def _task_names() -> frozenset[str]:
    """Return the closed set of registered Celery task names."""
    try:
        from config.celery import app as celery_app  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - celery config may be unavailable
        return frozenset()
    return frozenset(celery_app.tasks.keys())


def _task_name_value(value: object) -> str:
    """Validate a task label against registered Celery task names."""
    if isinstance(value, str) and value in _task_names():
        return value
    return INVALID_LABEL


def _queue_name_value(value: object) -> str:
    """Validate a queue label against configured Celery queue names."""
    if isinstance(value, str) and value in _queue_names(METRICS):
        return value
    return INVALID_LABEL


def _outbox_status_value(value: object) -> str:
    """Validate an outbox status label against the model's state set."""
    if isinstance(value, str) and value in OUTBOX_STATUSES:
        return value
    return INVALID_LABEL


def _scrape_source_value(value: object) -> str:
    """Validate a scrape-error source label against the collector set."""
    if isinstance(value, str) and value in SCRAPE_ERROR_SOURCES:
        return value
    return INVALID_LABEL


def _ai_outcome_value(value: object) -> str:
    """Validate an AI invocation outcome against the closed outcome set."""
    if isinstance(value, str) and value in AI_INVOCATION_OUTCOMES:
        return value
    return INVALID_LABEL


_AI_CAPABILITIES: set[str] = set()


def register_ai_capability(capability: str) -> None:
    """Register an AI capability key so its aggregates may be labeled."""
    cleaned = _machine_token(capability)
    if cleaned != INVALID_LABEL:
        _AI_CAPABILITIES.add(cleaned)


def _span_name_value(value: object) -> str:
    """Validate a span name: route names, internal names or task names."""
    if not isinstance(value, str):
        return INVALID_LABEL
    if value in _SPAN_INTERNAL_NAMES:
        return value
    if value in _route_names() or value in _task_names():
        return value
    return INVALID_LABEL


def _span_event_name_value(value: object) -> str:
    """Validate a span event name against the closed event set."""
    if isinstance(value, str) and value in _SPAN_EVENT_NAMES:
        return value
    return INVALID_LABEL


_SPAN_ATTRIBUTE_VALIDATORS: Final[dict[str, Callable[[object], object]]] = {
    "http.request.method": _method_value,
    "http.route": _route_name_value,
    "http.response.status_code": lambda value: _int_range(value, 100, 599),
    "clinic.request_id": _hex32,
    "clinic.queue": _queue_name_value,
    "clinic.task": _task_name_value,
    "clinic.operation_id": _uuid_value,
    "clinic.reason_code": _machine_token,
}


def _span_attribute_value(key: str, value: object) -> object:
    """Validate one allowlisted span attribute value by key."""
    validator = _SPAN_ATTRIBUTE_VALIDATORS.get(key)
    return validator(value) if validator is not None else INVALID_LABEL


_LOG_FIELD_VALIDATORS: Final[dict[str, Callable[[object], object]]] = {
    "request_id": _hex32,
    "route": _route_name_value,
    "method": _method_value,
    "status": lambda value: _int_range(value, 100, 599),
    "duration_ms": _number_field,
    "task": _task_name_value,
    "queue": _queue_name_value,
    "operation_id": _uuid_value,
    "reason_code": _machine_token,
    "event": _machine_token,
    "attempt": lambda value: _int_range(value, 0, 100),
}


def _log_field_value(field: str, value: object) -> object:
    """Validate one allowlisted log field value by field name."""
    validator = _LOG_FIELD_VALIDATORS.get(field)
    return validator(value) if validator is not None else INVALID_LABEL


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
    """Accept a strict-format inbound request id, else mint a fresh one."""
    candidate = request.META.get(_REQUEST_ID_HEADER, "")
    if isinstance(candidate, str) and _REQUEST_ID_FORMAT.fullmatch(candidate):
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

    The message must be a registered template from ``LOG_MESSAGE_ALLOWLIST``;
    ``args`` are never interpolated, so a caller that passes PHI as a
    positional argument or as the message itself cannot leak it. Exception
    output is the exception type name only — never the message or traceback.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Serialize the record; non-allowlisted extras are already gone."""
        template = str(record.msg)
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": _machine_token(record.name),
            "message": (
                template if template in LOG_MESSAGE_ALLOWLIST else UNLISTED_MESSAGE
            ),
        }
        for field in sorted(LOG_FIELD_ALLOWLIST):
            if field in vars(record):
                payload[field] = _log_field_value(field, getattr(record, field))
        request_id = _hex32(current_request_id())
        if request_id != INVALID_LABEL and "request_id" not in payload:
            payload["request_id"] = request_id
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception_type"] = _safe_type_name(record.exc_info[0].__name__)
        return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def _safe_type_name(value: object) -> str:
    """Coerce an exception type name; capitals are legitimate here."""
    text = redact_text(str(value))[:MAX_FIELD_VALUE_LENGTH]
    if _SAFE_TYPE_NAME.fullmatch(text):
        return text
    return INVALID_LABEL


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
    """Keep allowlisted span attributes; validate every value by key."""
    if not attributes:
        return {}
    filtered: dict[str, AttributeValue] = {}
    for key, value in attributes.items():
        if key not in SPAN_ATTRIBUTE_ALLOWLIST:
            continue
        cleaned = _span_attribute_value(key, value)
        if cleaned == INVALID_LABEL:
            continue
        filtered[key] = cast("AttributeValue", cleaned)
    return filtered


def _filtered_span(span: ReadableSpan) -> ReadableSpan:
    """Rebuild one span keeping only validated names and attributes.

    Status descriptions are dropped entirely: the SDK fills them with the
    exception message on error, which is exactly where clinical text would
    re-enter. Exception events keep only their name; their attributes are
    filtered like any other.
    """
    events = tuple(
        Event(
            name=_span_event_name_value(event.name),
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
        name=_span_name_value(span.name),
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=_filtered_attributes(span.attributes),
        events=events,
        links=links,
        kind=span.kind,
        instrumentation_scope=span.instrumentation_scope,
        status=Status(span.status.status_code),
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
        """Release nothing; the exporter holds no persistent resources."""

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
_ENQUEUED_AT_HEADER: Final = "clinic_enqueued_at"


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
    """In-process SLI registry; label values come from closed vocabularies."""

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
            route=_route_name_value(route),
            method=_method_value(method),
            status_class=_status_class_value(f"{status // 100}xx"),
        )
        with self._lock:
            histogram = self._request_histograms.setdefault(key, _Histogram())
            histogram.observe(seconds)

    def record_ai_invocation(
        self, *, capability: str, outcome: str, seconds: float, cost_micros: int
    ) -> None:
        """Aggregate one AI invocation by capability/outcome (todo 38 hook)."""
        key = (self._capability_label(capability), _ai_outcome_value(outcome))
        with self._lock:
            self._ai_invocations[key] = self._ai_invocations.get(key, 0) + 1
            self._ai_latency_sum[key] = self._ai_latency_sum.get(key, 0.0) + seconds
            self._ai_cost_sum[key] = self._ai_cost_sum.get(key, 0) + cost_micros

    def register_provider_probe(
        self, capability: str, probe: Callable[[], bool]
    ) -> None:
        """Register a provider health probe keyed by capability (todo 4 hook)."""
        cleaned = _machine_token(capability)
        if cleaned == INVALID_LABEL:
            return
        with self._lock:
            self._provider_probes[cleaned] = probe

    def _capability_label(self, capability: object) -> str:
        """Closed-set capability label: registered keys or probes only."""
        if not isinstance(capability, str):
            return INVALID_LABEL
        if capability in _AI_CAPABILITIES or capability in self._provider_probes:
            return capability
        return INVALID_LABEL

    def note_scrape_error(self, source: str) -> None:
        """Count one failed collector so silent gaps stay visible."""
        cleaned = _scrape_source_value(source)
        if cleaned == INVALID_LABEL:
            return
        with self._lock:
            self._scrape_errors[cleaned] = self._scrape_errors.get(cleaned, 0) + 1

    def render(self) -> str:
        """Render every family plus fresh collector output as text format."""
        sections = [
            self._render_request_histograms(),
            self._render_ai_invocations(),
            render_queue_metrics(registry=self),
            render_outbox_metrics(registry=self),
            self._render_scrape_errors(),
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


def _queue_names(registry: MetricsRegistry) -> frozenset[str]:
    """Resolve configured Celery queue names from the celery app config."""
    names: set[str] = set()
    try:
        from config.celery import app as celery_app  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - celery config must not break a scrape
        registry.note_scrape_error("queue-config")
        return frozenset()
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
    return frozenset(names)


def stamp_enqueue_timestamp(
    *,
    headers: dict[str, object] | None = None,
    **_kwargs: object,
) -> None:
    """Stamp the enqueue time on outgoing Celery message headers.

    Connected to ``celery.signals.before_task_publish`` in
    ``CoreConfig.ready``; protocol-2 headers are embedded in the broker
    message, so the metrics collector can read the real enqueue age of the
    oldest queued message without trusting message bodies.
    """
    if isinstance(headers, dict):
        headers[_ENQUEUED_AT_HEADER] = time.time()


def _message_enqueued_at(raw: object) -> float | None:
    """Extract the enqueue timestamp stamped by ``stamp_enqueue_timestamp``."""
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
            stamp = candidate.get(_ENQUEUED_AT_HEADER) or candidate.get("timestamp")
            if (
                isinstance(stamp, (int, float))
                and not isinstance(stamp, bool)
                and stamp > 0
            ):
                return float(stamp)
    return None


def render_queue_metrics(*, registry: MetricsRegistry = METRICS) -> str:
    """Scrape broker queue depth and oldest-message age per queue.

    Depth comes from ``LLEN``; age comes from the ``clinic_enqueued_at``
    header stamped at publish time. Messages published before the stamp
    existed simply omit the age series rather than fabricating one.
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
            for queue in sorted(_queue_names(registry)):
                depth = client.llen(queue)
                label = _queue_name_value(queue)
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
        registry.note_scrape_error("queue")
    return "\n".join(lines) + "\n"


def render_outbox_metrics(*, registry: MetricsRegistry = METRICS) -> str:
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
        registry.note_scrape_error("outbox")
        return "\n".join(lines) + "\n"
    now = datetime.now(tz=UTC)
    for status, count, oldest in rows:
        label = _outbox_status_value(status)
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
                route = _route_name(request)
                span.set_attribute("http.route", route)
                span.set_attribute("http.response.status_code", status)
                update_name = getattr(span, "update_name", None)
                if update_name is not None:
                    update_name(route)
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
    """Require a configured ASCII bearer token; fail closed when unset."""
    expected = os.environ.get(OPS_METRICS_TOKEN_ENV, "")
    if len(expected) < _MIN_TOKEN_LENGTH or not expected.isascii():
        return False
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if (
        not isinstance(header, str)
        or not header.isascii()
        or not header.startswith("Bearer ")
    ):
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
                key: _machine_token(value)
                for key, value in entry.items()
                if key in SENTRY_BREADCRUMB_ALLOWLIST
            }
        )
    return {"values": scrubbed}


def _scrub_trace_context(trace: object) -> dict[str, object]:
    """Keep only structural trace identifiers; drop ``data`` and the rest."""
    if not isinstance(trace, dict):
        return {}
    kept: dict[str, object] = {}
    for key, value in trace.items():
        if key not in SENTRY_TRACE_CONTEXT_ALLOWLIST:
            continue
        if key in {"trace_id", "span_id", "parent_span_id"}:
            cleaned = _hex32(value) if key == "trace_id" else _span_id(value)
            if cleaned != INVALID_LABEL:
                kept[key] = cleaned
        elif key == "op":
            kept[key] = _machine_token(value)
    return kept


def _span_id(value: object) -> str:
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{16}", value.lower()):
        return value.lower()
    return INVALID_LABEL


def _scrub_tags(tags: object) -> dict[str, object]:
    if not isinstance(tags, dict):
        return {}
    kept: dict[str, object] = {}
    for key, value in tags.items():
        if key not in SENTRY_TAG_ALLOWLIST:
            continue
        if key == "request_id":
            kept[key] = _hex32(value)
        elif key == "route":
            kept[key] = _route_name_value(value)
    return kept


def _scrub_request(request: object) -> dict[str, object]:
    if not isinstance(request, dict):
        return {}
    return {
        key: _method_value(value)
        for key, value in request.items()
        if key in SENTRY_REQUEST_ALLOWLIST
    }


def _sentry_timestamp(value: object) -> object:
    if isinstance(value, str) and _ISO_TIMESTAMP.match(value):
        return value
    if isinstance(value, bool):
        return INVALID_LABEL
    if isinstance(value, (int, float, datetime)):
        return value
    return INVALID_LABEL


def _sentry_level(value: object) -> object:
    return value if isinstance(value, str) and value in SENTRY_LEVELS else INVALID_LABEL


def _sentry_platform(value: object) -> object:
    return value if value == "python" else INVALID_LABEL


def _sentry_release(value: object) -> object:
    if isinstance(value, str) and _SHA_FORMAT.fullmatch(value.lower()):
        return value.lower()
    return _machine_token(value)


def _sentry_sdk(value: object) -> object:
    if not isinstance(value, dict):
        return {}
    return {
        key: _machine_token(item)
        for key, item in value.items()
        if key in {"name", "version"}
    }


_SENTRY_METADATA_VALIDATORS: Final[dict[str, Callable[[object], object]]] = {
    "event_id": _hex32,
    "timestamp": _sentry_timestamp,
    "level": _sentry_level,
    "platform": _sentry_platform,
    "release": _sentry_release,
    "transaction": _route_name_value,
    "logger": _machine_token,
    "environment": _machine_token,
    "server_name": _machine_token,
    "sdk": _sentry_sdk,
}


def _scrub_metadata_value(key: str, value: object) -> object:
    """Validate one allowlisted top-level metadata value by key."""
    validator = _SENTRY_METADATA_VALIDATORS.get(key)
    return validator(value) if validator is not None else INVALID_LABEL


def scrub_sentry_event(event: SentryEvent, _hint: SentryHint) -> SentryEvent:
    """Reduce one Sentry event to allowlisted, PHI-free fields.

    The surviving vocabulary is validated event metadata (ids, timestamps,
    level, release SHA), the exception *type* (never the message), the
    request *method* (never URL/headers/body), allowlisted tags such as
    ``request_id``, structural breadcrumbs and a trace context reduced to
    trace/span ids. Everything else — ``extra``, ``user``, ``message``,
    ``logentry``, ``modules``, ``fingerprint``, ``spans``, nested ``data`` —
    is dropped before the event leaves the process.
    """
    scrubbed: dict[str, object] = {}
    for key, value in dict(event).items():
        if key not in SENTRY_EVENT_ALLOWLIST:
            continue
        if key == "request":
            scrubbed[key] = _scrub_request(value)
        elif key == "tags":
            scrubbed[key] = _scrub_tags(value)
        elif key == "exception":
            scrubbed[key] = _scrub_exception(value)
        elif key == "breadcrumbs":
            scrubbed[key] = _scrub_breadcrumbs(value)
        elif key == "contexts" and isinstance(value, dict):
            trace = _scrub_trace_context(value.get("trace"))
            scrubbed[key] = {"trace": trace} if trace else {}
        else:
            cleaned = _scrub_metadata_value(key, value)
            if cleaned != INVALID_LABEL:
                scrubbed[key] = cleaned
    return cast("SentryEvent", scrubbed)
