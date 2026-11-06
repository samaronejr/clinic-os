"""Todo 50 portal results: only released sources open, and every download is audited.

``orders_patient_sources`` already decides the audience; these tests pin that
the portal neither widens it nor serves bytes without one chained
``orders.result.patient_downloaded`` event bound to the live patient session.
Unknown, unreleased and malformed selectors are one identical refusal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

import pytest
from apps.intake.patient_access import PATIENT_SESSION_KEY, patient_session_context
from apps.orders.patient_results import (
    DOWNLOAD_EVENT,
    patient_result_file,
    patient_results,
)
from apps.orders.services import acknowledge_result, ingest_result, release_result
from apps.orders.values import OrdersAccessDeniedError
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction
from django.test import Client
from django.utils import timezone

from ehr.test_autosave import as_actor
from orders.support import SOURCE, ordered, submission
from patient_http_support import audit_event_types, audit_payloads
from patient_service_support import runtime_role
from renewal.test_patient_access import _issue, _redeem

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

AUDIT_KEYS = frozenset(
    {
        "clinic_id",
        "http_method",
        "http_status",
        "object_verb",
        "reason_code",
        "request_id",
    }
)


def _patient_world(graph: RbacGraph, *, release: bool) -> tuple[UUID, UUID]:
    world = ordered(graph)
    with as_actor(graph, graph.physician):
        receipt = ingest_result(clinic_id=graph.clinic_a, submission=submission(world))
        if release:
            acknowledge_result(
                clinic_id=graph.clinic_a,
                version_id=receipt.version_id,
                disposition="no_action",
            )
            release_result(
                clinic_id=graph.clinic_a,
                version_id=receipt.version_id,
                audience="patient",
            )
    _, secret = _issue(graph, world.enrollment)
    session = _redeem(graph.clinic_a, secret)
    assert session is not None
    return session, receipt.version_id


def _downloads(graph: RbacGraph) -> list[str]:
    return [
        event
        for event in audit_event_types(graph, graph.clinic_admin)
        if event == DOWNLOAD_EVENT
    ]


def test_released_source_downloads_exact_bytes_with_one_session_bound_event(
    rbac_graph: RbacGraph,
) -> None:
    session, version = _patient_world(rbac_graph, release=True)
    with runtime_role(), patient_session_context(session):
        listed = patient_results()
        assert [(row.version_id, row.number, row.corrected) for row in listed] == [
            (version, 1, False)
        ]
        file = patient_result_file(version_id=version)
    assert file.data == SOURCE
    assert file.file_name == "resultado-1.pdf"
    assert _downloads(rbac_graph) == [DOWNLOAD_EVENT]
    payloads = audit_payloads(rbac_graph, rbac_graph.clinic_admin, DOWNLOAD_EVENT)
    assert {row["key"] for row in payloads} == {"clinic_id", "object_verb"}
    assert {row["key"] for row in payloads} <= AUDIT_KEYS
    # Outside a patient session the download scope is empty for the audit owner.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM clinic_app.orders_patient_download_scope(%s)",
            [version],
        )
        assert cursor.fetchone() == (0,)


def test_unreleased_unknown_and_staff_contexts_are_one_denial(
    rbac_graph: RbacGraph,
) -> None:
    session, version = _patient_world(rbac_graph, release=False)
    with runtime_role(), patient_session_context(session):
        assert patient_results() == ()
        for selected in (version, uuid4()):
            with pytest.raises(OrdersAccessDeniedError):
                patient_result_file(version_id=selected)
        with pytest.raises(OrdersAccessDeniedError):
            patient_result_file(version_id=cast("UUID", str(version)))
        with (
            pytest.raises(DatabaseError) as raised,
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT clinic_app.orders_patient_download_audit(%s,%s,%s)",
                [version, timezone.now(), b"\0" * 32],
            )
        assert raised.value.__cause__ is not None
        assert getattr(raised.value.__cause__, "sqlstate", None) == "42501"
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
        pytest.raises(OrdersAccessDeniedError),
    ):
        patient_result_file(version_id=version)
    assert _downloads(rbac_graph) == []


def test_portal_results_page_posts_selectors_and_refuses_identically(
    rbac_graph: RbacGraph,
) -> None:
    session, version = _patient_world(rbac_graph, release=True)
    client = Client()
    patient = client.session
    patient[PATIENT_SESSION_KEY] = str(session)
    patient.save()
    with runtime_role():
        page = client.get("/patient/results/")
        downloaded = client.post(
            "/patient/results/", {"action": "download", "version_id": str(version)}
        )
        refusals = [
            client.post("/patient/results/", body)
            for body in (
                {"action": "download", "version_id": str(uuid4())},
                {"action": "download", "version_id": "not-a-uuid"},
                {"action": "erase", "version_id": str(version)},
            )
        ]
    assert page.status_code == 200
    assert page.content.count(b'data-patient-result="') == 1
    assert str(version).encode() not in b"".join(
        line for line in page.content.splitlines() if b"href=" in line
    )
    assert downloaded.status_code == 200
    assert downloaded.content == SOURCE
    assert "no-store" in downloaded["Cache-Control"].split(", ")
    assert "private" in downloaded["Cache-Control"].split(", ")
    assert downloaded["Content-Disposition"] == 'attachment; filename="resultado-1.pdf"'
    assert [response.status_code for response in refusals] == [403, 403, 403]
    assert len({response.content for response in refusals}) == 1
    assert all("Set-Cookie" not in response.headers for response in refusals)
    assert _downloads(rbac_graph) == [DOWNLOAD_EVENT]
