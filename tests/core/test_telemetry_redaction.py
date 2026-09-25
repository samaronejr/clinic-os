"""PHI-redaction corpus tests for the telemetry boundary (ADR-014, todo 11).

Fifty synthetic PHI strings (``SINTETICO-SENTINELA-*`` ids, CPF-shaped
numbers, names, SOAP text, e-mails) are pushed through every egress channel —
log records, span attributes, metric labels and Sentry events — and none may
appear in captured output. The allowlist is the boundary: only allowlisted
fields/attributes/labels survive at all, and their values are additionally
pattern-redacted and charset-restricted.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
from contextlib import contextmanager
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

from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from opentelemetry.sdk.trace import ReadableSpan
    from sentry_sdk.types import Event as SentryEvent

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


class _ExportError(RuntimeError):
    pass


class _ProbeError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Synthetic PHI corpus: exactly 50 strings, all clearly non-real.
# ---------------------------------------------------------------------------

_SENTINEL_IDS: Final = tuple(f"SINTETICO-SENTINELA-{index:04d}" for index in range(15))
_CPF_SHAPED: Final = tuple(
    f"{100 + index:03d}.{200 + index:03d}.{300 + index:03d}-{index:02d}"
    for index in range(10)
) + tuple(f"{10_000_000_000 + index}" for index in range(5))
_NAMES: Final = tuple(
    f"sintetico paciente {index} SINTETICO-SENTINELA-N{index:02d}"
    for index in range(10)
)
_SOAP_NOTES: Final = tuple(
    "SINTETICO-SENTINELA-SOAP-"
    f"{index:02d} S: cefaleia O: PA 120x80 A: enxaqueca P: sintomatico"
    for index in range(5)
)
_EMAILS: Final = tuple(
    f"sintetico-sentinela-{index}@mail.invalid" for index in range(5)
)

PHI_CORPUS: Final = _SENTINEL_IDS + _CPF_SHAPED + _NAMES + _SOAP_NOTES + _EMAILS

assert len(PHI_CORPUS) == 50

# Atoms that must never appear in any captured output, even partially.
_FORBIDDEN_ATOMS: Final = (
    "SINTETICO-SENTINELA",
    "sintetico-sentinela",
    "sintetico paciente",
    "cefaleia",
    "enxaqueca",
    "mail.invalid",
    *_CPF_SHAPED,
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


def test_log_channel_drops_all_50_phi_strings(captured_log: io.StringIO) -> None:
    probe = logging.getLogger("telemetry-corpus")
    for entry in PHI_CORPUS:
        # args are never interpolated into the emitted template.
        probe.info("telemetry probe %s", entry)
        # non-allowlisted extras are dropped before formatting.
        probe.info("telemetry probe", extra={"payload": entry, "notes": entry})
        # allowlisted extras are charset-cleaned to a placeholder.
        probe.info("telemetry probe", extra={"reason_code": entry})
        # exception text never leaves; only the type name survives.
        _raise_and_log(probe, entry)
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


def test_log_record_carries_request_id_from_context(
    captured_log: io.StringIO,
) -> None:
    probe = logging.getLogger("telemetry-corpus")
    token = telemetry._current_request_id.set("req-abc123")
    try:
        probe.info("inside request")
    finally:
        telemetry._current_request_id.reset(token)
    (line,) = [json.loads(item) for item in captured_log.getvalue().splitlines()]
    assert line["request_id"] == "req-abc123"


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


def test_span_channel_drops_all_50_phi_strings() -> None:
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
    with tracer.start_as_current_span("probe") as span:
        span.set_attribute("clinic.request_id", "req-1")
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


def test_metric_labels_drop_all_50_phi_strings() -> None:
    registry = telemetry.MetricsRegistry()
    for entry in PHI_CORPUS:
        registry.observe_request(route=entry, method="get", status=200, seconds=0.01)
        registry.record_ai_invocation(
            capability=entry, outcome="ok", seconds=0.01, cost_micros=1
        )
        registry.register_provider_probe(entry, lambda: True)
    rendered = (
        registry._render_request_histograms()
        + registry._render_ai_invocations()
        + registry._render_provider_health()
    )
    _assert_no_phi(rendered)
    assert telemetry.INVALID_LABEL in rendered


def test_metrics_render_contains_sli_families() -> None:
    registry = telemetry.MetricsRegistry()
    registry.observe_request(route="agenda-day", method="get", status=200, seconds=0.05)
    registry.record_ai_invocation(
        capability="visit-brief", outcome="ok", seconds=1.5, cost_micros=42
    )
    rendered = registry.render()
    assert "clinic_http_request_duration_seconds_bucket" in rendered
    assert 'route="agenda-day"' in rendered
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


def test_queue_metrics_survive_broker_outage() -> None:
    with override_settings(CELERY_BROKER_URL="redis://127.0.0.1:1/0"):
        rendered = telemetry.render_queue_metrics()
    assert "clinic_queue_depth" in rendered
    registry = telemetry.MetricsRegistry()
    registry.note_scrape_error("queue")
    assert 'source="queue"' in registry._render_scrape_errors()


def test_queue_metrics_parse_timestamped_message() -> None:
    stamped = json.dumps({"headers": {"timestamp": 1_700_000_000.0}, "properties": {}})
    assert telemetry._message_enqueued_at(stamped) == 1_700_000_000.0
    assert telemetry._message_enqueued_at("not json") is None
    assert telemetry._message_enqueued_at(json.dumps({"headers": {}})) is None


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


def test_sentry_event_with_soap_exception_keeps_only_type_and_request_id() -> None:
    soap = _SOAP_NOTES[0]
    event: SentryEvent = {
        "event_id": "a" * 32,
        "level": "error",
        "transaction": "/patients/123/encounter",
        "tags": {"request_id": "req-xyz", "patient": soap},
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
    assert scrubbed["tags"] == {"request_id": "req-xyz"}
    assert scrubbed["request"] == {"method": "post"}
    assert "extra" not in scrubbed
    assert "user" not in scrubbed
    assert "message" not in scrubbed


def test_sentry_scrubber_drops_all_50_phi_strings() -> None:
    for entry in PHI_CORPUS:
        event: SentryEvent = {
            "event_id": "b" * 32,
            "exception": {"values": [{"type": "E", "value": entry}]},
            "request": {"method": "GET", "url": entry, "data": entry},
            "tags": {"request_id": "req-1", "note": entry},
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


def test_middleware_propagates_and_echoes_request_id() -> None:
    factory = RequestFactory()
    seen: list[str] = []

    def view(request: HttpRequest) -> HttpResponse:
        seen.append(telemetry.current_request_id())
        return _ok_view(request)

    middleware = telemetry.TelemetryMiddleware(view)
    request = factory.get("/probe", headers={"X-Request-ID": "req-fixed-1"})
    response = middleware(request)
    assert seen == ["req-fixed-1"]
    assert response["X-Request-ID"] == "req-fixed-1"


def test_middleware_mints_request_id_and_rejects_bad_inbound() -> None:
    factory = RequestFactory()
    seen: list[str] = []

    def view(request: HttpRequest) -> HttpResponse:
        seen.append(telemetry.current_request_id())
        return _ok_view(request)

    middleware = telemetry.TelemetryMiddleware(view)
    request = factory.get("/probe", headers={"X-Request-ID": "bad id!"})
    response = middleware(request)
    assert seen
    assert seen[0] != "bad id!"
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", seen[0])
    assert response["X-Request-ID"] == seen[0]


def test_middleware_records_histogram_by_route_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = telemetry.MetricsRegistry()
    monkeypatch.setattr(telemetry, "METRICS", registry)
    factory = RequestFactory()
    request = factory.get("/patients/12345/secret")
    request.resolver_match = ResolverMatch(_ok_view, (), {}, url_name="patient-detail")
    middleware = telemetry.TelemetryMiddleware(_ok_view)
    middleware(request)
    rendered = registry._render_request_histograms()
    assert 'route="patient-detail"' in rendered
    assert "12345" not in rendered
    assert "secret" not in rendered
