"""Todo 55 pages over HTTP on real roles: urgent queue, coverage and notices.

Selectors travel in POST bodies; unknown selectors and roles without the page's
permission get the shared 403; the patient portal states only configured terms.
"""

from __future__ import annotations

from datetime import time
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.intake.patient_access import PATIENT_SESSION_KEY
from apps.tenancy.db import tenant_context
from apps.workflows.coordination_views import CONFLICT, COVERAGE_INPUT
from apps.workflows.urgent import NO_COVERAGE, set_urgent_coverage
from apps.workflows.urgent_models import UrgentConcern
from django.conf import settings
from django.test import Client
from django.urls import reverse
from django.utils.translation import gettext

from comms.support import manager_of
from core.test_navigation import _client_for, _get
from identity.permission_support import owner_context
from otp_test_support import fixed_otp_time
from patient_service_support import runtime_role
from workflows.coordination_support import (
    NOTICE,
    URGENT,
    patient_reply,
    urgent_world,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
OK = 200
BAD_REQUEST = 400
FORBIDDEN = 403
CONFLICTED = 409


def _url(graph: RbacGraph, name: str) -> str:
    return reverse(f"workflows:{name}", args=(graph.clinic_a,))


def _post(
    client: Client, path: str, data: Mapping[str, str | list[str]]
) -> _MonkeyPatchedWSGIResponse:
    form: dict[str, str | list[str]] = {
        "csrfmiddlewaretoken": client.cookies[settings.CSRF_COOKIE_NAME].value
    }
    form.update(data)
    with runtime_role(), fixed_otp_time():
        return client.post(path, form)


def _html(response: _MonkeyPatchedWSGIResponse, status: int = OK) -> str:
    assert response.status_code == status
    return response.content.decode()


def test_urgent_queue_page_decides_by_post_body_and_never_shows_the_text(
    rbac_graph: RbacGraph,
) -> None:
    world = urgent_world(rbac_graph, roles=("nurse",))
    graph = world.graph
    patient_reply(world, URGENT)
    with owner_context(graph.organization_a):
        concern = UrgentConcern.objects.get()
    nurse, _user = _client_for(graph, "nurse")
    url = _url(graph, "urgent-concerns")
    page = _get(nurse, url)
    html = _html(page)
    assert {"private", "no-store"} <= {
        part.strip() for part in page["Cache-Control"].split(",")
    }
    assert gettext("Urgent patient message") in html
    assert str(concern.pk) in html
    assert URGENT not in html
    selector = {"concern_id": str(concern.pk)}
    assert _post(nurse, url, {"action": "ack"}).status_code == FORBIDDEN
    unknown = {"action": "ack", "concern_id": str(uuid4()), "revision": "1"}
    assert _post(nurse, url, unknown).status_code == FORBIDDEN
    acked = _html(_post(nurse, url, {**selector, "action": "ack", "revision": "1"}))
    assert gettext(
        "Concern acknowledged. Reply to the patient in the conversation."
    ) in (acked)
    stale = _post(nurse, url, {**selector, "action": "ack", "revision": "1"})
    assert gettext(CONFLICT) in _html(stale, CONFLICTED)
    resolved = _post(nurse, url, {**selector, "action": "resolve", "revision": "2"})
    assert gettext("Concern resolved.") in _html(resolved)
    assert gettext("No urgent concerns are waiting.") in resolved.content.decode()
    finance, _user = _client_for(graph, "finance")
    assert _get(finance, url).status_code == FORBIDDEN
    assert _post(
        finance, url, {**selector, "action": "ack", "revision": "3"}
    ).status_code == (FORBIDDEN)


def test_coverage_page_publishes_versions_and_refuses_bad_or_stale_terms(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    admin, _user = _client_for(graph, "clinic_admin")
    url = _url(graph, "urgent-coverage")
    assert gettext(NO_COVERAGE) in _html(_get(admin, url))
    terms: dict[str, str | list[str]] = {
        "action": "publish",
        "expected_version": "0",
        "weekdays": ["0", "1", "2", "3", "4"],
        "opens_at": "08:00",
        "closes_at": "18:00",
        "response_hours": "4",
        "responsible_roles": ["nurse", "physician"],
    }
    # Without the explicit action the post is the shared refusal.
    assert _post(admin, url, {**terms, "action": "save"}).status_code == FORBIDDEN
    inverted = {**terms, "opens_at": "18:00", "closes_at": "08:00"}
    assert gettext(COVERAGE_INPUT) in _html(_post(admin, url, inverted), BAD_REQUEST)
    timezone_bearing = {**terms, "opens_at": "0900Z"}
    assert gettext(COVERAGE_INPUT) in _html(
        _post(admin, url, timezone_bearing), BAD_REQUEST
    )
    unknown_role = {**terms, "responsible_roles": ["owner"]}
    assert gettext(COVERAGE_INPUT) in _html(
        _post(admin, url, unknown_role), BAD_REQUEST
    )
    published = _html(_post(admin, url, terms))
    assert gettext("Coverage published. Patients now see these hours.") in published
    assert "08:00" in published
    assert "18:00" in published
    assert gettext(CONFLICT) in _html(_post(admin, url, terms), CONFLICTED)
    physician, _user = _client_for(graph, "physician")
    assert _get(physician, url).status_code == FORBIDDEN
    assert _post(physician, url, {**terms, "expected_version": "1"}).status_code == (
        FORBIDDEN
    )


def test_release_notice_page_is_clinician_approved_and_identifier_free(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    physician, _user = _client_for(graph, "physician")
    url = _url(graph, "release-notices")
    empty = _html(_get(physician, url))
    assert (
        gettext(
            "No notice is approved yet. Results cannot be released to patients until "
            "a clinician approves one."
        )
        in empty
    )
    publish = {"action": "publish"}
    assert _post(physician, url, {"body": NOTICE}).status_code == FORBIDDEN
    refused = _html(
        _post(physician, url, {**publish, "body": "Ligue 5555-0000."}), BAD_REQUEST
    )
    assert "5555" not in refused.split("</textarea>", 1)[0].rsplit("<textarea", 1)[1]
    approved = _html(_post(physician, url, {**publish, "body": NOTICE}))
    assert gettext("Notice approved. New releases use this version.") in approved
    assert NOTICE in approved
    admin, _user = _client_for(graph, "clinic_admin")
    assert _get(admin, url).status_code == FORBIDDEN
    assert _post(admin, url, {**publish, "body": NOTICE}).status_code == FORBIDDEN


def test_patient_messages_page_states_only_the_configured_coverage(
    rbac_graph: RbacGraph,
) -> None:
    world = urgent_world(rbac_graph, coverage=False)
    client = Client()
    stored = client.session
    stored[PATIENT_SESSION_KEY] = str(world.session)
    stored.save()
    with runtime_role():
        before = client.get("/patient/messages/")
    assert gettext(NO_COVERAGE) in _html(before)
    # The graph's clinic_admin administers clinic B; clinic A needs its own admin.
    manager = manager_of(rbac_graph)
    with (
        runtime_role(),
        tenant_context(manager, rbac_graph.organization_a),
    ):
        set_urgent_coverage(
            clinic_id=rbac_graph.clinic_a,
            expected_version=0,
            weekdays=(5, 6),
            opens_at=time(9, 0),
            closes_at=time(12, 0),
            response_hours=1,
            responsible_roles=("nurse",),
        )
    with runtime_role():
        after = _html(client.get("/patient/messages/"))
        thread = _html(
            client.post(
                "/patient/messages/",
                {"action": "open", "conversation_id": str(world.conversation)},
            )
        )
    assert gettext(NO_COVERAGE) not in after
    for html in (after, thread):
        assert "09:00" in html
        assert "12:00" in html
        assert "192" in html
