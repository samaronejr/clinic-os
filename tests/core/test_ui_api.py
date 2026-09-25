"""Internal UI API: session + CSRF, body-only identifiers, error contract."""

from __future__ import annotations

import io
import json
import logging
import secrets
from contextlib import contextmanager
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.core.middleware import CSP_HEADER
from apps.identity.models import User
from apps.tenancy.db import tenant_context
from django.conf import settings
from django.test import Client

from otp_test_support import OTP_RAW_CREDENTIAL, create_receptionist, runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.test.client import _MonkeyPatchedWSGIResponse

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

AGENDA_QUERY = "/api/ui/v1/agenda/query/"
APPOINTMENT_DAY = "2035-06-02"


def _error(code: str) -> dict[str, str]:
    return {"code": code, "message_key": f"api.error.{code}"}


class ApiSession:
    """A CSRF-enforcing client signed in through the real session backend."""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.token = secrets.token_hex(16)
        client.cookies[settings.CSRF_COOKIE_NAME] = self.token

    def post(
        self,
        body: bytes | str,
        *,
        csrf: str | None = "cookie",
        content_type: str = "application/json",
        path: str = AGENDA_QUERY,
    ) -> _MonkeyPatchedWSGIResponse:
        headers = (
            {}
            if csrf is None
            else {"X-CSRFToken": self.token if csrf == "cookie" else csrf}
        )
        return self.client.post(
            path,
            data=body,
            content_type=content_type,
            headers=headers,
        )

    def query(self, clinic_id: UUID, **overrides: object) -> _MonkeyPatchedWSGIResponse:
        body: dict[str, object] = {
            "clinic_id": str(clinic_id),
            "view": "day",
            "date": APPOINTMENT_DAY,
        }
        body.update(overrides)
        return self.post(json.dumps(body))


def _signed_in(username: str, password: str) -> ApiSession:
    # A request exception must surface as the API's JSON body, never as a
    # re-raised test-client exception, so the contract itself is asserted.
    client = Client(enforce_csrf_checks=True, raise_request_exception=False)
    assert client.login(username=username, password=password)
    return ApiSession(client)


@pytest.fixture
def receptionist_api(rbac_graph: RbacGraph) -> Iterator[ApiSession]:
    receptionist = create_receptionist(rbac_graph)
    with runtime_role():
        yield _signed_in(receptionist.username, OTP_RAW_CREDENTIAL)


def _json(response: _MonkeyPatchedWSGIResponse) -> object:
    assert response["Content-Type"] == "application/json"
    return json.loads(response.content)


def test_agenda_query_adapts_the_service_page(rbac_graph: RbacGraph) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment = create_synthetic_appointment(setup)
    receptionist = create_receptionist(rbac_graph)

    with runtime_role():
        api = _signed_in(receptionist.username, OTP_RAW_CREDENTIAL)
        response = api.query(rbac_graph.clinic_a)

    assert response.status_code == 200
    assert response.headers[CSP_HEADER].startswith("default-src 'self'")
    assert "no-store" in response.headers["Cache-Control"]
    page = _json(response)
    assert isinstance(page, dict)
    assert set(page) == {
        "items",
        "view",
        "date",
        "page",
        "total",
        "page_count",
        "start_at",
        "end_at",
    }
    assert (page["view"], page["date"], page["page"]) == ("day", APPOINTMENT_DAY, 1)
    assert (page["total"], page["page_count"]) == (1, 1)
    [item] = page["items"]
    assert item["appointment_id"] == str(appointment.pk)
    assert item["practitioner_id"] == str(setup.practitioner_id)
    assert item["patient_display_name"] == "Synthetic Booking Persona"
    assert item["start_local"].endswith("09:00")
    assert item["status"] == "scheduled"


def test_post_without_csrf_header_is_refused(receptionist_api: ApiSession) -> None:
    response = receptionist_api.post(json.dumps({}), csrf=None)

    assert response.status_code == 403
    assert _json(response) == _error("csrf_failed")


def test_post_with_a_foreign_csrf_token_is_refused(
    receptionist_api: ApiSession,
) -> None:
    response = receptionist_api.post(json.dumps({}), csrf=secrets.token_hex(16))

    assert response.status_code == 403
    assert _json(response) == _error("csrf_failed")


def test_anonymous_post_gets_the_json_denial() -> None:
    response = Client().post(AGENDA_QUERY, data="{}", content_type="application/json")

    assert response.status_code == 403
    assert _json(response) == _error("access_denied")


def test_unknown_and_foreign_clinics_share_one_denial(
    rbac_graph: RbacGraph,
    receptionist_api: ApiSession,
) -> None:
    unknown = receptionist_api.query(uuid4())
    other_org = receptionist_api.query(rbac_graph.clinic_c)
    unassigned = receptionist_api.query(rbac_graph.clinic_b)

    responses = (unknown, other_org, unassigned)
    assert {response.status_code for response in responses} == {403}
    assert len({response.content for response in responses}) == 1
    assert _json(unknown) == _error("access_denied")


def test_malformed_json_body_is_a_stable_error(receptionist_api: ApiSession) -> None:
    response = receptionist_api.post(b'{"clinic_id": "SINTETICO-SENTINELA-1",')

    assert response.status_code == 400
    assert _json(response) == _error("malformed_body")
    assert b"SINTETICO-SENTINELA" not in response.content


@pytest.mark.parametrize(
    "body",
    [
        "[]",
        '"SINTETICO-SENTINELA-2"',
        json.dumps({"view": "day", "date": APPOINTMENT_DAY}),
        json.dumps(
            {
                "clinic_id": "SINTETICO-SENTINELA-3",
                "view": "day",
                "date": APPOINTMENT_DAY,
            }
        ),
    ],
)
def test_invalid_body_shapes_are_rejected_without_reflection(
    receptionist_api: ApiSession,
    body: str,
) -> None:
    response = receptionist_api.post(body)

    assert response.status_code == 400
    assert _json(response) == _error("invalid_input")
    assert b"SINTETICO-SENTINELA" not in response.content


@pytest.mark.parametrize(
    "overrides",
    [
        {"view": "month"},
        {"date": "2035-02-30"},
        {"page": 0},
        {"page": True},
        # Boundary values the service cannot represent (gate review B2).
        {"date": "9999-12-31"},
        {"page": 2**63},
        # Fuzzed extremes around the supported window and page bound.
        {"date": "0001-01-01"},
        {"date": "1999-12-31"},
        {"date": "2200-01-01"},
        {"view": "week", "date": "9999-12-31"},
        {"page": 10_001},
        {"page": 2**31},
        {"page": -(2**63)},
        {"page": 10**100},
        {"page": 1.5},
        {"page": "1e3"},
        {"date": "2035-06-02T00:00:00"},
        {"date": "+2035-06-02"},
    ],
)
def test_invalid_query_values_are_rejected(
    rbac_graph: RbacGraph,
    receptionist_api: ApiSession,
    overrides: dict[str, object],
) -> None:
    response = receptionist_api.query(rbac_graph.clinic_a, **overrides)

    assert response.status_code == 400
    assert _json(response) == _error("invalid_input")


def test_form_encoded_body_is_unsupported(receptionist_api: ApiSession) -> None:
    response = receptionist_api.post(
        "clinic_id=x", content_type="application/x-www-form-urlencoded"
    )

    assert response.status_code == 415
    assert _json(response) == _error("unsupported_media_type")


def test_get_is_not_part_of_the_contract(
    rbac_graph: RbacGraph,
    receptionist_api: ApiSession,
) -> None:
    response = receptionist_api.client.get(
        AGENDA_QUERY, {"clinic_id": str(rbac_graph.clinic_a)}
    )

    assert response.status_code == 405
    assert _json(response) == _error("method_not_allowed")


def test_privileged_actor_without_totp_needs_step_up(rbac_graph: RbacGraph) -> None:
    physician = User.objects.get(pk=rbac_graph.physician)
    with runtime_role():
        api = _signed_in(physician.username, RBAC_RAW_CREDENTIAL)
        response = api.query(rbac_graph.clinic_a)

    assert response.status_code == 403
    assert _json(response) == _error("step_up_required")


@pytest.mark.parametrize(
    "overrides",
    [
        {"date": "2000-01-01"},
        {"date": "2199-12-31"},
        {"view": "week", "date": "2199-12-31"},
        {"page": 10_000},
    ],
)
def test_supported_window_edges_are_served(
    rbac_graph: RbacGraph,
    receptionist_api: ApiSession,
    overrides: dict[str, object],
) -> None:
    response = receptionist_api.query(rbac_graph.clinic_a, **overrides)

    assert response.status_code == 200
    page = _json(response)
    assert isinstance(page, dict)
    assert page["items"] == []


@contextmanager
def _configured_console_output() -> Iterator[io.StringIO]:
    """Redirect the settings-configured root ``console`` handler to memory.

    The handler, its formatter, filters and logger routing stay exactly as
    configured; only the destination stream changes, then is restored.
    """
    [handler] = [
        handler
        for handler in logging.getLogger().handlers
        if handler.get_name() == "console"
    ]
    assert isinstance(handler, logging.StreamHandler)
    stream = io.StringIO()
    previous = handler.setStream(stream)
    try:
        yield stream
    finally:
        handler.setStream(previous)


def test_unexpected_failure_is_a_json_error_logged_without_phi(
    rbac_graph: RbacGraph,
    receptionist_api: ApiSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "SINTETICO-SENTINELA-500"
    cause = "SINTETICO-SENTINELA-CAUSA"

    def fail_underneath() -> None:
        raise ValueError(cause)

    def explode(**_scope: object) -> None:
        try:
            fail_underneath()
        except ValueError as error:
            raise RuntimeError(sentinel, {"patient": sentinel}) from error

    monkeypatch.setattr("apps.core.api.views.view_agenda", explode)
    with (
        caplog.at_level(logging.INFO),
        _configured_console_output() as console,
    ):
        response = receptionist_api.query(rbac_graph.clinic_a)

    assert response.status_code == 500
    assert _json(response) == _error("internal_error")
    assert sentinel.encode() not in response.content
    assert response.headers[CSP_HEADER].startswith("default-src 'self'")
    [record] = [r for r in caplog.records if r.name == "django.request"]
    assert record.levelno == logging.ERROR
    rendered = console.getvalue()
    assert rendered, "the 500 must still be logged through the configured handler"
    for output in (rendered, caplog.text, record.getMessage()):
        assert "SINTETICO-SENTINELA" not in output
        assert "route=ui_api:agenda-query" in output
        assert "exception=builtins.RuntimeError" in output
        assert "test_ui_api.py:" in output
    assert record.exc_info is None


@pytest.mark.parametrize(
    "path",
    [
        "/api/ui/v1/not-a-route/",
        "/api/ui/v1/agenda/query",
        "/api/ui/v1/agenda/query/extra/",
        "/api/ui/v1/",
    ],
)
def test_unknown_routes_get_the_json_not_found_body(
    receptionist_api: ApiSession,
    path: str,
) -> None:
    posted = receptionist_api.post(json.dumps({}), path=path)
    without_csrf = receptionist_api.post(json.dumps({}), path=path, csrf=None)
    fetched = receptionist_api.client.get(path)

    for response in (posted, without_csrf, fetched):
        assert response.status_code == 404
        assert _json(response) == _error("not_found")


def test_anonymous_unknown_route_is_still_the_tenant_denial() -> None:
    response = Client().post(
        "/api/ui/v1/not-a-route/", data="{}", content_type="application/json"
    )

    assert response.status_code == 403
    assert _json(response) == _error("access_denied")
