"""DG-R1: the represented patient's access log shows each delegate read."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import psycopg
import pytest
from apps.ehr.finalization import finalize_version
from apps.identity.delegates import switch_delegate
from apps.intake.models import Patient
from apps.intake.patient_access import PATIENT_SESSION_KEY, patient_session_context
from apps.orders.queries import patient_sources
from apps.orders.services import (
    IdentityEvidence,
    acknowledge_result,
    ingest_result,
    place_order,
    release_result,
)
from apps.retention.services import patient_released_records, release_version
from apps.tenancy.db import tenant_context
from django.test import Client

from auth.stepup_test_support import verified_request
from ehr.longitudinal_support import chart_world
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from orders.support import World, submission
from patient_service_support import runtime_role
from patients.delegate_support import NotesFixture, active_grant, delegate_world
from patients.test_delegates import _switch
from renewal.test_patient_access import _issue, _redeem

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
ACCESS_LOG = "/patient/security/access-log/"
OK = 200
# The default grant (which also carries enrollment_view) and the narrowest grant
# that still lets both readers return content: the log must follow the readers.
OPERATIONS: Final = pytest.mark.parametrize(
    "operations",
    [("enrollment_view", "booking", "records", "consent"), ("records",)],
    ids=["default-grant", "records-only"],
)


class _Entries(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.entries: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "li" and any(name == "data-access-kind" for name, _ in attrs):
            self.entries.append(dict(attrs))


def _client(session_id: UUID) -> Client:
    client = Client()
    session = client.session
    session[PATIENT_SESSION_KEY] = str(session_id)
    session.save()
    return client


def _patient_log(graph: RbacGraph, enrollment_id: UUID) -> list[tuple[str | None, ...]]:
    """Open the represented patient's own access log and return its entries."""
    _, secret = _issue(graph, enrollment_id)
    patient = _redeem(graph.clinic_a, secret)
    assert patient is not None
    with runtime_role():
        response = _client(patient).get(ACCESS_LOG)
    assert response.status_code == OK
    parser = _Entries()
    parser.feed(response.content.decode())
    return [
        (
            item["data-access-kind"],
            item.get("data-record-type"),
            item.get("data-delegate-grant"),
        )
        for item in parser.entries
    ]


def _stored(url: str) -> list[tuple[object, ...]]:
    with psycopg.connect(url) as database:
        return database.execute(
            "SELECT grant_id, enrollment_id, record_type "
            "FROM clinic_app.identity_delegateaccessevent"
        ).fetchall()


@OPERATIONS
def test_delegate_read_appears_in_the_represented_patients_access_log(
    rbac_graph: RbacGraph,
    tmp_path: Path,
    superuser_database_url: str,
    operations: tuple[str, ...],
) -> None:
    # Given a released record and an active guardian acting for the patient.
    world = delegate_world(rbac_graph, tmp_path)
    request = verified_request(rbac_graph.physician)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        final = finalize_version(
            clinic_id=rbac_graph.clinic_a,
            version_id=world.notes.first_version_id,
            expected_revision=2,
            request=request,
        )
        release_version(
            clinic_id=rbac_graph.clinic_a,
            version_id=final.pk,
            restricted_from_delegates=False,
        )
    grant = active_grant(rbac_graph, world, operations=operations)
    _switch(rbac_graph, world, grant)
    # When the delegate reads the patient's released records.
    with runtime_role(), patient_session_context(world.session_id):
        assert patient_released_records()
    # Then the represented patient's own log lists that read: delegate access,
    # which grant, and the record type ...
    assert _patient_log(rbac_graph, world.notes.enrollment_id) == [
        ("delegate", "clinical_record", str(grant.grant_id))
    ]
    # ... while the delegate session itself still cannot open that log, and the
    # stored row is content-free metadata.
    with runtime_role():
        assert _client(world.session_id).get(ACCESS_LOG).status_code != OK
    assert _stored(superuser_database_url) == [
        (grant.grant_id, world.notes.enrollment_id, "clinical_record")
    ]


@OPERATIONS
def test_delegate_result_read_appears_in_the_represented_patients_access_log(
    rbac_graph: RbacGraph,
    tmp_path: Path,
    superuser_database_url: str,
    operations: tuple[str, ...],
) -> None:
    # Given a result released to the patient and to one guardian's grant.
    graph = rbac_graph
    chart = chart_world(graph)
    with owner_context(graph.organization_a):
        patient = Patient.objects.get(pk=chart.patient)
        identity = IdentityEvidence(
            birth_date=patient.birth_date, name=patient.full_name
        )
        notes = NotesFixture(
            chart.enrollment,
            chart.patient,
            chart.version.document.encounter_id,
            chart.version.pk,
        )
    delegate = delegate_world(graph, tmp_path, notes=notes)
    grant = active_grant(graph, delegate, operations=operations)
    with as_actor(graph, graph.physician):
        order = place_order(
            clinic_id=graph.clinic_a,
            enrollment_id=chart.enrollment,
            exam_codes=("cbc",),
            command_id=uuid4(),
        )
        receipt = ingest_result(
            clinic_id=graph.clinic_a,
            submission=submission(World(order, identity, chart.enrollment)),
        )
        acknowledge_result(
            clinic_id=graph.clinic_a,
            version_id=receipt.version_id,
            disposition="no_action",
        )
        release_result(
            clinic_id=graph.clinic_a, version_id=receipt.version_id, audience="patient"
        )
        release_result(
            clinic_id=graph.clinic_a,
            version_id=receipt.version_id,
            audience="delegate",
            delegate_grant_id=grant.grant_id,
        )
    with runtime_role(), patient_session_context(delegate.session_id):
        switch_delegate(clinic_id=graph.clinic_a, grant_id=grant.grant_id)
    # When the delegate reads the released result source.
    with runtime_role(), patient_session_context(delegate.session_id):
        assert len(patient_sources(version_id=receipt.version_id)) == 1
    # Then the represented patient's own log lists that read as a result.
    assert _patient_log(graph, chart.enrollment) == [
        ("delegate", "result", str(grant.grant_id))
    ]
    assert _stored(superuser_database_url) == [
        (grant.grant_id, chart.enrollment, "result")
    ]
