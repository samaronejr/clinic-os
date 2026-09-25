"""PHI-redaction corpus tests for the telemetry boundary (ADR-014, todo 11).

A synthetic PHI corpus (``SINTETICO-SENTINELA-*`` ids, CPF shapes, names,
SOAP text, e-mails, UUIDs, phone shapes, bearer tokens, token-shaped names)
is pushed through every egress channel — log records, span names and
attributes, metric labels, the metrics endpoint and Sentry events — and
none may appear in captured output. The boundary is a closed vocabulary:
only registered message templates, allowlisted fields with per-field
validators, closed-set labels and validated identifiers survive at all.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import pytest
from apps.comms.models import IntegrationOperation
from apps.core import telemetry
from apps.tenancy.db import tenant_context
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory, override_settings
from django.urls.resolvers import ResolverMatch
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from typing import Self

    from opentelemetry.sdk.trace import ReadableSpan
    from sentry_sdk.types import Event as SentryEvent

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


class _ExportError(RuntimeError):
    pass


class _ProbeError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Synthetic PHI corpus: reviewer probes plus the original shapes.
# ---------------------------------------------------------------------------

_SENTINEL_IDS: Final = tuple(f"SINTETICO-SENTINELA-{index:04d}" for index in range(15))
_CPF_SHAPED: Final = tuple(
    f"{100 + index:03d}.{200 + index:03d}.{300 + index:03d}-{index:02d}"
    for index in range(10)
) + tuple(f"{10_000_000_000 + index}" for index in range(5))
_NAMES: Final = (
    *(
        f"sintetico paciente {index} SINTETICO-SENTINELA-N{index:02d}"
        for index in range(10)
    ),
    "Mariana Souza",
)
_SOAP_NOTES: Final = tuple(
    "SINTETICO-SENTINELA-SOAP-"
    f"{index:02d} S: cefaleia O: PA 120x80 A: enxaqueca P: sintomatico"
    for index in range(5)
)
_EMAILS: Final = tuple(
    f"sintetico-sentinela-{index}@mail.invalid" for index in range(5)
)
_UUIDS: Final = tuple(
    f"{letter * 8}-1111-4222-8333-444444444444" for letter in ("a", "b", "c", "d", "e")
)
_BARE_HEX_IDS: Final = ("f" * 32, "0123456789abcdef0123456789abcdef")
_PHONES: Final = ("+55 11 91234-5678", "(11) 99876-5432", "61987654321")
_BEARER_TOKENS: Final = (
    "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature",
    "Bearer sk-live-abcdef1234567890",
)
_TOKEN_SHAPED_NAMES: Final = (
    "sintetico-aurora-revisao",
    "camila-rocha-viana",
    "sintetico-patient-full-name",
)

PHI_CORPUS: Final = (
    _SENTINEL_IDS
    + _CPF_SHAPED
    + _NAMES
    + _SOAP_NOTES
    + _EMAILS
    + _UUIDS
    + _BARE_HEX_IDS
    + _PHONES
    + _BEARER_TOKENS
    + _TOKEN_SHAPED_NAMES
)

# Atoms that must never appear in any captured output, even partially.
_FORBIDDEN_ATOMS: Final = (
    "SINTETICO-SENTINELA",
    "sintetico-sentinela",
    "sintetico paciente",
    "Mariana Souza",
    "cefaleia",
    "enxaqueca",
    "mail.invalid",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
    "sk-live-abcdef1234567890",
    "camila-rocha-viana",
    "sintetico-aurora-revisao",
    "91234-5678",
    "99876-5432",
    *_CPF_SHAPED,
    *_UUIDS,
)


def _assert_no_phi(output: str) -> None:
    for atom in _FORBIDDEN_ATOMS:
        assert atom not in output


def _raise_phi(entry: str) -> None:
    raise ValueError(entry)


def _raise_and_log(probe: logging.Logger, entry: str) -> None:
    try:
        _raise_phi(entry)
    except ValueError:
        probe.exception("telemetry probe failed")


# ---------------------------------------------------------------------------
# Structured logging channel.
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_log() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(telemetry.JsonTelemetryFormatter())
    handler.addFilter(telemetry.AllowlistLogFilter())
    probe = logging.getLogger("telemetry-corpus")
    probe.addHandler(handler)
    probe.setLevel(logging.DEBUG)
    probe.propagate = False
    try:
        yield stream
    finally:
        probe.removeHandler(handler)


def test_log_channel_drops_all_phi_strings(captured_log: io.StringIO) -> None:
    probe = logging.getLogger("telemetry-corpus")
    for entry in PHI_CORPUS:
        # args are never interpolated into the emitted template.
        probe.info("telemetry probe %s", entry)
        # non-allowlisted extras are dropped before formatting.
        probe.info("telemetry probe", extra={"payload": entry, "notes": entry})
        # allowlisted extras are validated per field and collapse.
        probe.info("telemetry probe", extra={"reason_code": entry})
        # exception text never leaves; only the type name survives.
        _raise_and_log(probe, entry)
    _assert_no_phi(captured_log.getvalue())


def test_log_literal_clinical_message_collapses(captured_log: io.StringIO) -> None:
    # The reviewer repro: clinical text passed as the *message* must not be
    # emitted; unlisted templates render as a fixed sentinel.
    probe = logging.getLogger("telemetry-corpus")
    for entry in _NAMES + _SOAP_NOTES + _BEARER_TOKENS:
        probe.info(entry)
    lines = [json.loads(line) for line in captured_log.getvalue().splitlines()]
    assert all(line["message"] == "[unlisted]" for line in lines)
    _assert_no_phi(captured_log.getvalue())


def test_log_message_template_is_pattern_redacted(
    captured_log: io.StringIO,
) -> None:
    probe = logging.getLogger("telemetry-corpus")
    probe.info("sentinel %s", "unused")
    probe.info("literal SINTETICO-SENTINELA-9999 in template")
    probe.info("cpf 123.456.789-09 in template")
    output = captured_log.getvalue()
    _assert_no_phi(output)
    assert "123.456.789-09" not in output
    lines = [json.loads(line) for line in output.splitlines()]
    assert all("payload" not in line for line in lines)
    assert all(isinstance(line["message"], str) for line in lines)


def test_log_registered_template_passes(captured_log: io.StringIO) -> None:
    logging.getLogger("telemetry-corpus").info("http request")
    (line,) = [json.loads(item) for item in captured_log.getvalue().splitlines()]
    assert line["message"] == "http request"


def test_log_numeric_phi_shapes_collapse(captured_log: io.StringIO) -> None:
    # The reviewer repro: numeric CPF shapes arrive as ints or digit strings.
    probe = logging.getLogger("telemetry-corpus")
    probe.info("http request", extra={"status": 12345678901})
    probe.info("http request", extra={"duration_ms": "12345678901"})
    lines = [json.loads(line) for line in captured_log.getvalue().splitlines()]
    assert lines[0]["status"] == "[invalid]"
    assert lines[1]["duration_ms"] == "[invalid]"
    _assert_no_phi(captured_log.getvalue())


def test_log_token_shaped_reason_codes_collapse(captured_log: io.StringIO) -> None:
    # The reviewer repro: token-shaped person names in open fields.
    probe = logging.getLogger("telemetry-corpus")
    for entry in _TOKEN_SHAPED_NAMES + _UUIDS + _BARE_HEX_IDS:
        probe.info("http request", extra={"reason_code": entry})
    lines = [json.loads(line) for line in captured_log.getvalue().splitlines()]
    assert all(line["reason_code"] == "[invalid]" for line in lines)
    _assert_no_phi(captured_log.getvalue())


def test_log_uuid_fields_validate_strictly(captured_log: io.StringIO) -> None:
    probe = logging.getLogger("telemetry-corpus")
    probe.info("http request", extra={"request_id": "req-" + "b" * 28})
    probe.info(
        "http request",
        extra={"operation_id": "aaaaaaaa-1111-4222-8333-444444444444"},
    )
    probe.info("http request", extra={"operation_id": "Mariana Souza"})
    lines = [json.loads(line) for line in captured_log.getvalue().splitlines()]
    assert lines[0]["request_id"] == "[invalid]"
    assert lines[1]["operation_id"] == "aaaaaaaa-1111-4222-8333-444444444444"
    assert lines[2]["operation_id"] == "[invalid]"


def test_log_record_carries_request_id_from_context(
    captured_log: io.StringIO,
) -> None:
    probe = logging.getLogger("telemetry-corpus")
    token = telemetry._current_request_id.set("c" * 32)
    try:
        probe.info("http request")
    finally:
        telemetry._current_request_id.reset(token)
    (line,) = [json.loads(item) for item in captured_log.getvalue().splitlines()]
    assert line["request_id"] == "c" * 32


# ---------------------------------------------------------------------------
# Span channel.
# ---------------------------------------------------------------------------


def _in_memory_provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(telemetry.AllowlistSpanExporter(memory))
    )
    return provider, memory


def test_span_channel_drops_all_phi_strings() -> None:
    provider, memory = _in_memory_provider()
    tracer = provider.get_tracer("telemetry-corpus")
    for entry in PHI_CORPUS:
        span = tracer.start_span("probe")
        span.set_attribute("http.route", entry)
        span.set_attribute("patient.name", entry)
        span.set_attribute("clinic.request_id", entry)
        span.add_event("probe-event", attributes={"soap": entry})
        span.end()
    provider.shutdown()
    serialized = json.dumps(
        [span.to_json() for span in memory.get_finished_spans()], default=str
    )
    _assert_no_phi(serialized)
    for finished in memory.get_finished_spans():
        assert set(finished.attributes or {}) <= telemetry.SPAN_ATTRIBUTE_ALLOWLIST


def test_span_status_description_never_exports_exception_message() -> None:
    # The reviewer repro: record_exception + StatusCode.ERROR carried the
    # exception message into the exported span's status description.
    provider, memory = _in_memory_provider()
    tracer = provider.get_tracer("telemetry-corpus")
    soap = _SOAP_NOTES[0]

    def _boom() -> None:
        raise ValueError(soap)

    with tracer.start_as_current_span("http.request") as span:
        try:
            _boom()
        except ValueError as exc:
            span.record_exception(exc)
            span.set_status(
                StatusCode.ERROR,
                description=f"boom {soap} /api/patients/{_UUIDS[0]}",
            )
    provider.shutdown()
    (finished,) = memory.get_finished_spans()
    blob = finished.to_json()
    _assert_no_phi(blob)
    assert "description" not in blob
    # Automatic exception attributes carry the message/stacktrace; all gone.
    assert "exception.message" not in blob
    assert "exception.stacktrace" not in blob
    assert finished.events[0].name == "exception"


def test_span_names_must_be_routes_or_tasks() -> None:
    provider, memory = _in_memory_provider()
    tracer = provider.get_tracer("telemetry-corpus")
    for name in (
        "Mariana Souza",
        "/api/patients/" + _UUIDS[0],
        "camila-rocha-viana",
        "healthz",
        "http.request",
    ):
        tracer.start_span(name).end()
    provider.shutdown()
    names = [span.name for span in memory.get_finished_spans()]
    assert names == ["[invalid]", "[invalid]", "[invalid]", "healthz", "http.request"]


def test_span_event_names_are_closed() -> None:
    provider, memory = _in_memory_provider()
    tracer = provider.get_tracer("telemetry-corpus")
    span = tracer.start_span("http.request")
    span.add_event("exception")
    span.add_event("cefaleia note SINTETICO-SENTINELA-0004")
    span.end()
    provider.shutdown()
    (finished,) = memory.get_finished_spans()
    assert [event.name for event in finished.events] == ["exception", "[invalid]"]
    _assert_no_phi(finished.to_json())


def test_tracing_disabled_by_default() -> None:
    assert telemetry.build_tracer_provider({}) is None
    assert telemetry.build_tracer_provider({"CLINIC_OTEL_ENABLED": "false"}) is None


def test_tracing_rejects_unknown_exporter() -> None:
    with pytest.raises(ImproperlyConfigured):
        telemetry.build_tracer_provider(
            {"CLINIC_OTEL_ENABLED": "1", "CLINIC_OTEL_EXPORTER": "bogus"}
        )


def test_otlp_exporter_failure_returns_failure_not_exception() -> None:
    """A dead collector endpoint yields FAILURE, never an exception."""
    exporter = telemetry.OtlpJsonSpanExporter("http://127.0.0.1:1")
    provider, memory = _in_memory_provider()
    tracer = provider.get_tracer("telemetry-corpus")
    span = tracer.start_span("probe")
    span.end()
    finished = memory.get_finished_spans()
    assert exporter.export(finished) == SpanExportResult.FAILURE
    provider.shutdown()


def test_enabled_provider_with_dead_endpoint_does_not_raise() -> None:
    """Exporter endpoint down -> the application path is unaffected."""
    provider = telemetry.build_tracer_provider(
        {
            "CLINIC_OTEL_ENABLED": "1",
            "CLINIC_OTEL_EXPORTER": "otlp-http-json",
            "CLINIC_OTEL_OTLP_ENDPOINT": "http://127.0.0.1:1",
        }
    )
    assert provider is not None
    tracer = provider.get_tracer("telemetry-corpus")
    with tracer.start_as_current_span("http.request") as span:
        span.set_attribute("clinic.request_id", "f" * 32)
    # force_flush exercises the export path synchronously; it must not raise.
    provider.force_flush(timeout_millis=5000)
    provider.shutdown()


def test_wrapped_exporter_exception_is_contained() -> None:
    class _Exploding(SpanExporter):
        def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
            raise _ExportError

    exporter = telemetry.AllowlistSpanExporter(_Exploding())
    assert exporter.export(()) == SpanExportResult.FAILURE


def test_otlp_endpoint_must_be_http_url() -> None:
    with pytest.raises(ImproperlyConfigured):
        telemetry.OtlpJsonSpanExporter("not-a-url")


# ---------------------------------------------------------------------------
# Metrics channel.
# ---------------------------------------------------------------------------


def test_metric_labels_drop_all_phi_strings() -> None:
    registry = telemetry.MetricsRegistry()
    for entry in PHI_CORPUS:
        registry.observe_request(route=entry, method="get", status=200, seconds=0.01)
        registry.record_ai_invocation(
            capability=entry, outcome="ok", seconds=0.01, cost_micros=1
        )
        registry.register_provider_probe(entry, lambda: True)
        registry.note_scrape_error(entry)
    rendered = (
        registry._render_request_histograms()
        + registry._render_ai_invocations()
        + registry._render_provider_health()
        + registry._render_scrape_errors()
    )
    _assert_no_phi(rendered)
    assert telemetry.INVALID_LABEL in rendered


def test_metric_route_label_is_a_closed_set() -> None:
    # The reviewer repro: patient UUIDs and token-shaped names were accepted
    # as route labels. Only registered URL names (or "unmatched") survive.
    registry = telemetry.MetricsRegistry()
    for entry in _UUIDS + _TOKEN_SHAPED_NAMES + ("12345678901", "98765432100"):
        registry.observe_request(route=entry, method="get", status=200, seconds=0.01)
    registry.observe_request(route="healthz", method="get", status=200, seconds=0.01)
    registry.observe_request(
        route="/api/patients/1", method="get", status=404, seconds=0.01
    )
    rendered = registry._render_request_histograms()
    _assert_no_phi(rendered)
    assert 'route="healthz"' in rendered
    assert 'route="[invalid]"' in rendered
    assert "/api/patients" not in rendered


def test_capability_and_outcome_labels_are_closed() -> None:
    registry = telemetry.MetricsRegistry()
    telemetry.register_ai_capability("summarize-note")
    registry.record_ai_invocation(
        capability="summarize-note", outcome="success", seconds=0.2, cost_micros=10
    )
    registry.record_ai_invocation(
        capability="camila-rocha-viana", outcome="success", seconds=0.2, cost_micros=10
    )
    registry.record_ai_invocation(
        capability="summarize-note", outcome="bogus", seconds=0.2, cost_micros=10
    )
    rendered = registry._render_ai_invocations()
    assert 'capability="summarize-note",outcome="success"' in rendered
    assert 'capability="[invalid]"' in rendered
    assert 'outcome="[invalid]"' in rendered
    _assert_no_phi(rendered)


def test_metrics_render_contains_sli_families() -> None:
    registry = telemetry.MetricsRegistry()
    registry.observe_request(route="index", method="get", status=200, seconds=0.05)
    telemetry.register_ai_capability("visit-brief")
    registry.record_ai_invocation(
        capability="visit-brief", outcome="success", seconds=1.5, cost_micros=42
    )
    rendered = registry.render()
    assert "clinic_http_request_duration_seconds_bucket" in rendered
    assert 'route="index"' in rendered
    assert 'clinic_ai_invocations_total{capability="visit-brief"' in rendered
    assert "clinic_queue_depth" in rendered
    assert "clinic_outbox_operations" in rendered
    assert "clinic_provider_health" in rendered


def test_provider_probe_crash_reports_unhealthy() -> None:
    registry = telemetry.MetricsRegistry()

    def _boom() -> bool:
        raise _ProbeError

    registry.register_provider_probe("synthetic-cap", _boom)
    rendered = registry._render_provider_health()
    assert 'clinic_provider_health{capability="synthetic-cap"} 0' in rendered


def test_outbox_status_labels_are_model_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: list[tuple[object, ...]] = [
        ("pending", 2, None),
        ("SINTETICO-SENTINELA-0001", 9, None),
        ("camila-rocha-viana", 4, None),
    ]
    monkeypatch.setattr(telemetry, "connection", _FakeConnection(_FakeCursor(rows)))
    rendered = telemetry.render_outbox_metrics(registry=telemetry.MetricsRegistry())
    assert 'clinic_outbox_operations{status="pending"}' in rendered
    assert 'clinic_outbox_operations{status="[invalid]"}' in rendered
    _assert_no_phi(rendered)


def test_outbox_metrics_report_status_aggregates(rbac_graph: RbacGraph) -> None:
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        IntegrationOperation.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            actor_id=rbac_graph.physician,
            channel="sms",
            provider="synthetic",
            subject_type="probe",
            subject_id=uuid4(),
            status="pending",
            idempotency_key=uuid4(),
        )
    with runtime_role():
        rendered = telemetry.render_outbox_metrics()
    assert 'clinic_outbox_operations{status="pending"}' in rendered
    assert "clinic_outbox_oldest_age_seconds" in rendered


class _FakeCursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, _sql: str) -> None:
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor


def test_queue_metrics_survive_broker_outage() -> None:
    with override_settings(CELERY_BROKER_URL="redis://127.0.0.1:1/0"):
        registry = telemetry.MetricsRegistry()
        rendered = telemetry.render_queue_metrics(registry=registry)
    assert "clinic_queue_depth" in rendered
    assert 'source="queue"' in registry._render_scrape_errors()


def test_publish_stamp_enables_queue_age() -> None:
    # B5: before_task_publish stamps the header; the collector reads it
    # back from the broker message, so normal tasks produce an age sample.
    headers: dict[str, object] = {}
    telemetry.stamp_enqueue_timestamp(headers=headers)
    stamp = headers[telemetry._ENQUEUED_AT_HEADER]
    assert isinstance(stamp, float)
    assert stamp <= time.time()
    message = json.dumps({"headers": headers, "properties": {}})
    assert telemetry._message_enqueued_at(message) == stamp


def test_message_enqueued_at_accepts_legacy_timestamp_header() -> None:
    stamped = json.dumps({"headers": {"timestamp": 1_700_000_000.0}, "properties": {}})
    assert telemetry._message_enqueued_at(stamped) == 1_700_000_000.0
    assert telemetry._message_enqueued_at("not json") is None
    assert telemetry._message_enqueued_at(json.dumps({"headers": {}})) is None


def test_queue_metrics_emit_age_for_stamped_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers: dict[str, object] = {}
    telemetry.stamp_enqueue_timestamp(headers=headers)
    payload = json.dumps({"headers": headers, "properties": {}}).encode()

    class _FakeRedis:
        def llen(self, _queue: str) -> int:
            return 3

        def lindex(self, _queue: str, _index: int) -> bytes:
            return payload

        def close(self) -> None:
            return None

    import redis  # noqa: PLC0415

    monkeypatch.setattr(redis.Redis, "from_url", lambda *_a, **_k: _FakeRedis())
    monkeypatch.setattr(
        telemetry, "_queue_names", lambda _registry=None: frozenset({"celery"})
    )
    with override_settings(CELERY_BROKER_URL="redis://127.0.0.1:6379/0"):
        rendered = telemetry.render_queue_metrics(registry=telemetry.MetricsRegistry())
    assert 'clinic_queue_depth{queue="celery"} 3' in rendered
    assert 'clinic_queue_oldest_age_seconds{queue="celery"}' in rendered
    _assert_no_phi(rendered)


# ---------------------------------------------------------------------------
# /internal/metrics endpoint auth (ops token + network restriction).
# ---------------------------------------------------------------------------

_OPS_TOKEN: Final = f"synthetic-ops-{uuid4().hex}"


@contextmanager
def _ops_token_env() -> Iterator[None]:
    previous = os.environ.get(telemetry.OPS_METRICS_TOKEN_ENV)
    os.environ[telemetry.OPS_METRICS_TOKEN_ENV] = _OPS_TOKEN
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(telemetry.OPS_METRICS_TOKEN_ENV, None)
        else:
            os.environ[telemetry.OPS_METRICS_TOKEN_ENV] = previous


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_metrics_endpoint_denies_without_token() -> None:
    os.environ.pop(telemetry.OPS_METRICS_TOKEN_ENV, None)
    client = Client()
    response = client.get("/internal/metrics")
    assert response.status_code == 401


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_metrics_endpoint_denies_bad_token() -> None:
    client = Client()
    with _ops_token_env():
        response = client.get(
            "/internal/metrics", headers={"Authorization": "Bearer wrong-token"}
        )
    assert response.status_code == 401


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_metrics_endpoint_rejects_non_ascii_bearer() -> None:
    # The reviewer note: a non-ASCII bearer used to raise a 500 inside
    # compare_digest; it must be a plain 401.
    client = Client()
    with _ops_token_env():
        response = client.get(
            "/internal/metrics",
            headers={"Authorization": "Bearer té☃oken-abcdefghijklm"},
        )
    assert response.status_code == 401


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_metrics_endpoint_denies_unlisted_network() -> None:
    client = Client()
    with _ops_token_env():
        response = client.get(
            "/internal/metrics",
            headers={"Authorization": f"Bearer {_OPS_TOKEN}"},
            REMOTE_ADDR="192.0.2.10",
        )
    assert response.status_code == 404


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_metrics_endpoint_serves_prometheus_text() -> None:
    client = Client()
    with _ops_token_env():
        response = client.get(
            "/internal/metrics",
            headers={"Authorization": f"Bearer {_OPS_TOKEN}"},
        )
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/plain")
    body = response.content.decode()
    assert "clinic_http_request_duration_seconds" in body
    _assert_no_phi(body)


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_metrics_endpoint_rejects_post() -> None:
    client = Client()
    with _ops_token_env():
        response = client.post(
            "/internal/metrics",
            headers={"Authorization": f"Bearer {_OPS_TOKEN}"},
        )
    assert response.status_code == 405


# ---------------------------------------------------------------------------
# Sentry scrubber.
# ---------------------------------------------------------------------------


def test_sentry_event_with_soap_exception_keeps_only_type() -> None:
    soap = _SOAP_NOTES[0]
    event: SentryEvent = {
        "event_id": "a" * 32,
        "level": "error",
        "transaction": "/patients/123/encounter",
        "tags": {"request_id": "b" * 32, "patient": soap},
        "request": {
            "method": "POST",
            "url": f"https://example.invalid/{soap}",
            "headers": {"Authorization": f"Bearer {soap}"},
            "data": {"note": soap},
            "cookies": {"session": soap},
        },
        "exception": {
            "values": [
                {
                    "type": "ClinicalNoteError",
                    "value": soap,
                    "stacktrace": {"frames": [{"vars": {"note": soap}}]},
                }
            ]
        },
        "breadcrumbs": {"values": [{"type": "http", "message": soap}]},
        "extra": {"soap": soap},
        "user": {"username": soap},
        "message": soap,
    }
    scrubbed = telemetry.scrub_sentry_event(event, {})
    serialized = json.dumps(scrubbed, default=str)
    _assert_no_phi(serialized)
    exception = scrubbed["exception"]
    assert isinstance(exception, dict)
    (entry,) = exception["values"]
    assert entry == {"type": "ClinicalNoteError"}
    assert scrubbed["tags"] == {"request_id": "b" * 32}
    assert scrubbed["request"] == {"method": "post"}
    assert "extra" not in scrubbed
    assert "user" not in scrubbed
    assert "message" not in scrubbed


def test_sentry_event_scrubs_nested_payload_recursively() -> None:
    # The reviewer's nested payload: clinical data in every pocket.
    event: SentryEvent = {
        "event_id": "e" * 32,
        "timestamp": datetime(2026, 9, 25, tzinfo=UTC),
        "level": "error",
        "logger": "apps.ehr",
        "platform": "python",
        "transaction": "/api/patients/SINTETICO-SENTINELA-0100",
        "message": "patient Mariana Souza cefaleia",
        "logentry": {"formatted": "SOAP SINTETICO-SENTINELA-0101"},
        "user": {"email": "sintetico-sentinela-1@mail.invalid"},
        "extra": {"note": "metformina 850mg"},
        "fingerprint": ["SINTETICO-SENTINELA-0102"],
        "modules": {"django": "5.2"},
        "tags": {
            "request_id": "req-aaaaaaaa-1111-4222-8333-444444444444",
            "route": "healthz",
            "patient": "Mariana Souza",
        },
        "request": {
            "method": "POST",
            "url": "http://clinic.test/api/patients/SINTETICO-SENTINELA-0103",
            "headers": {"Authorization": _BEARER_TOKENS[0]},
            "data": {"soap": "cefaleia"},
        },
        "exception": {
            "values": [
                {
                    "type": "ValueError",
                    "value": "patient SINTETICO-SENTINELA-0104 cefaleia",
                    "mechanism": {"type": "celery"},
                }
            ]
        },
        "breadcrumbs": {
            "values": [
                {
                    "type": "http",
                    "category": "request",
                    "level": "info",
                    "data": {"url": "/patients/SINTETICO-SENTINELA-0105"},
                }
            ]
        },
        "contexts": {
            "trace": {
                "trace_id": "f" * 32,
                "span_id": "0123456789abcdef",
                "op": "http.server",
                "data": {"soap": "SINTETICO-SENTINELA-0106"},
            },
            "patient": {"name": "Mariana Souza"},
        },
        "spans": [{"description": "SELECT * FROM patients WHERE cpf=12345678901"}],
    }
    scrubbed = telemetry.scrub_sentry_event(event, {})
    blob = json.dumps(scrubbed, default=str)
    _assert_no_phi(blob)
    for dropped in (
        "extra",
        "user",
        "message",
        "logentry",
        "modules",
        "fingerprint",
        "spans",
    ):
        assert dropped not in scrubbed
    assert scrubbed["request"] == {"method": "post"}
    # contexts.trace survives reduced to structural ids + op only.
    trace_ctx = scrubbed["contexts"]["trace"]
    assert set(trace_ctx) <= {"trace_id", "span_id", "parent_span_id", "op"}
    assert trace_ctx["trace_id"] == "f" * 32
    assert "data" not in trace_ctx
    assert "patient" not in scrubbed["contexts"]
    # Nested tag values are validated, not copied verbatim.
    assert scrubbed["tags"]["request_id"] == "[invalid]"
    assert scrubbed["tags"]["route"] == "healthz"
    assert "patient" not in scrubbed["tags"]
    # Exception keeps type only.
    (entry,) = scrubbed["exception"]["values"]
    assert set(entry) <= {"type", "mechanism"}
    # transaction was a raw URL; not a route name -> collapses.
    assert scrubbed.get("transaction") != "/api/patients/SINTETICO-SENTINELA-0100"


def test_sentry_scrubber_drops_all_phi_strings() -> None:
    for entry in PHI_CORPUS:
        event: SentryEvent = {
            "event_id": "b" * 32,
            "exception": {"values": [{"type": "E", "value": entry}]},
            "request": {"method": "GET", "url": entry, "data": entry},
            "tags": {"request_id": entry, "note": entry},
            "extra": {"payload": entry},
            "breadcrumbs": {"values": [{"message": entry}]},
            "message": entry,
        }
        serialized = json.dumps(telemetry.scrub_sentry_event(event, {}), default=str)
        for atom in _FORBIDDEN_ATOMS:
            assert atom not in serialized


# ---------------------------------------------------------------------------
# Middleware: request-id propagation and SLI recording.
# ---------------------------------------------------------------------------


def _ok_view(_request: HttpRequest) -> HttpResponse:
    return HttpResponse("ok")


def test_middleware_propagates_and_echoes_valid_request_id() -> None:
    factory = RequestFactory()
    seen: list[str] = []

    def view(request: HttpRequest) -> HttpResponse:
        seen.append(telemetry.current_request_id())
        return _ok_view(request)

    middleware = telemetry.TelemetryMiddleware(view)
    inbound = "d" * 32
    request = factory.get("/probe", headers={"X-Request-ID": inbound})
    response = middleware(request)
    assert seen == [inbound]
    assert response["X-Request-ID"] == inbound


def test_middleware_mints_request_id_and_rejects_bad_inbound() -> None:
    factory = RequestFactory()
    seen: list[str] = []

    def view(request: HttpRequest) -> HttpResponse:
        seen.append(telemetry.current_request_id())
        return _ok_view(request)

    middleware = telemetry.TelemetryMiddleware(view)
    for bad in (
        "bad id!",
        "req-abc123",
        "SINTETICO-SENTINELA-0001",
        "aaaaaaaa-1111-4222-8333-444444444444",
        "Z" * 32,
    ):
        request = factory.get("/probe", headers={"X-Request-ID": bad})
        response = middleware(request)
        assert seen
        issued = seen[-1]
        assert issued != bad
        assert re.fullmatch(r"[0-9a-f]{32}", issued)
        assert response["X-Request-ID"] == issued


def test_middleware_records_histogram_by_route_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = telemetry.MetricsRegistry()
    monkeypatch.setattr(telemetry, "METRICS", registry)
    factory = RequestFactory()
    request = factory.get("/healthz")
    request.resolver_match = ResolverMatch(_ok_view, (), {}, url_name="healthz")
    middleware = telemetry.TelemetryMiddleware(_ok_view)
    middleware(request)
    rendered = registry._render_request_histograms()
    assert 'route="healthz"' in rendered


def test_middleware_unmatched_route_uses_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = telemetry.MetricsRegistry()
    monkeypatch.setattr(telemetry, "METRICS", registry)
    factory = RequestFactory()
    request = factory.get("/patients/12345/secret")
    middleware = telemetry.TelemetryMiddleware(_ok_view)
    middleware(request)
    rendered = registry._render_request_histograms()
    assert 'route="unmatched"' in rendered
    assert "12345" not in rendered
    assert "secret" not in rendered


# ---------------------------------------------------------------------------
# Gunicorn access-log contract (reviewer B1).
# ---------------------------------------------------------------------------


def test_gunicorn_access_log_format_is_phi_free() -> None:
    from pathlib import Path  # noqa: PLC0415

    config = (
        Path(__file__).parents[2] / "ops" / "container" / "gunicorn_no_proxy.py"
    ).read_text()
    assert 'access_log_format = "%(m)s %(s)s %(D)s"' in config
    for atom in ("%(r)s", "%(U)s", "%(q)s", "%(h)s", "%(f)s", "%(u)s"):
        assert atom not in config
    assert "%({" not in config


def test_supervised_argv_uses_phi_free_access_log() -> None:
    import sys  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from ops.testing.browser_server_supervisor import supervised_argv  # noqa: PLC0415

    argv = supervised_argv(Path(sys.executable), 58419, Path("a.log"))
    index = argv.index("--access-logformat")
    assert argv[index + 1] == "%(m)s %(s)s %(D)s"
