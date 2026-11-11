"""DG-R1: the represented patient's access log shows each delegate read."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import TYPE_CHECKING

import psycopg
import pytest
from apps.ehr.finalization import finalize_version
from apps.intake.patient_access import PATIENT_SESSION_KEY, patient_session_context
from apps.retention.services import patient_released_records, release_version
from apps.tenancy.db import tenant_context
from django.test import Client

from auth.stepup_test_support import verified_request
from patient_service_support import runtime_role
from patients.delegate_support import active_grant, delegate_world
from patients.test_delegates import _switch
from renewal.test_patient_access import _issue, _redeem

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
ACCESS_LOG = "/patient/security/access-log/"
OK = 200


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


def test_delegate_read_appears_in_the_represented_patients_access_log(
    rbac_graph: RbacGraph, tmp_path: Path, superuser_database_url: str
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
    grant = active_grant(rbac_graph, world)
    _switch(rbac_graph, world, grant)
    # When the delegate reads the patient's released records.
    with runtime_role(), patient_session_context(world.session_id):
        assert patient_released_records()
    # Then the represented patient's own log lists that read: delegate access,
    # which grant, and the record type ...
    _, secret = _issue(rbac_graph, world.notes.enrollment_id)
    patient = _redeem(rbac_graph.clinic_a, secret)
    assert patient is not None
    with runtime_role():
        response = _client(patient).get(ACCESS_LOG)
    assert response.status_code == OK
    parser = _Entries()
    parser.feed(response.content.decode())
    assert [
        (
            item["data-access-kind"],
            item.get("data-record-type"),
            item.get("data-delegate-grant"),
        )
        for item in parser.entries
    ] == [("delegate", "clinical_record", str(grant.grant_id))]
    # ... while the delegate session itself still cannot open that log, and the
    # stored row is content-free metadata.
    with runtime_role():
        assert _client(world.session_id).get(ACCESS_LOG).status_code != OK
    with psycopg.connect(superuser_database_url) as database:
        rows = database.execute(
            "SELECT grant_id, enrollment_id, record_type "
            "FROM clinic_app.identity_delegateaccessevent"
        ).fetchall()
    assert rows == [(grant.grant_id, world.notes.enrollment_id, "clinical_record")]
