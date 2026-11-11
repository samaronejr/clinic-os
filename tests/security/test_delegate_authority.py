"""DG-D1 and DG-R1: who may revoke a delegate grant, and what a switch records."""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg
import pytest
from apps.identity.delegates import revoke
from apps.identity.models import PatientDelegateGrant
from apps.intake.access import PatientAccessDeniedError
from apps.intake.patient_access import patient_session_context

from identity.permission_support import permission_context
from patient_service_support import runtime_role
from patients.delegate_support import active_grant, delegate_world
from patients.test_delegates import _switch
from renewal.test_encounters import setup_context
from renewal.test_patient_access import _issue, _redeem

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    from apps.identity.delegates import DelegateReceipt

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
PAYLOAD_KEYS = {
    "clinic_id",
    "http_method",
    "http_status",
    "object_verb",
    "reason_code",
    "request_id",
}


def _state(graph: RbacGraph, grant_id: UUID) -> str:
    with setup_context(graph.organization_a):
        return str(PatientDelegateGrant.objects.get(pk=grant_id).state)


def test_only_verifier_staff_or_the_grantor_can_revoke_a_delegate_grant(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    # Given an active guardian grant and the represented patient's own session.
    world = delegate_world(rbac_graph, tmp_path)
    grant = active_grant(rbac_graph, world)
    _, secret = _issue(rbac_graph, world.notes.enrollment_id)
    grantor = _redeem(rbac_graph.clinic_a, secret)
    assert grantor is not None

    def command() -> DelegateReceipt:
        return revoke(
            clinic_id=rbac_graph.clinic_a,
            grant_id=grant.grant_id,
            expected_revision=grant.revision,
        )

    # When the delegate's own session or staff without clinic configuration
    # authority try to revoke it, both are refused and nothing changes.
    with (
        runtime_role(),
        patient_session_context(world.session_id),
        pytest.raises(PatientAccessDeniedError),
    ):
        command()
    with (
        permission_context(rbac_graph, rbac_graph.physician),
        pytest.raises(PatientAccessDeniedError),
    ):
        command()
    assert _state(rbac_graph, grant.grant_id) == "active"
    # Then the grantor can revoke it.
    with runtime_role(), patient_session_context(grantor):
        receipt = command()
    assert receipt.state == "revoked"
    assert _state(rbac_graph, grant.grant_id) == "revoked"


def test_delegate_session_switch_appends_a_metadata_only_audit_event(
    rbac_graph: RbacGraph, tmp_path: Path, superuser_database_url: str
) -> None:
    # Given an active grant, when the delegate session acts for the patient.
    world = delegate_world(rbac_graph, tmp_path)
    grant = active_grant(rbac_graph, world)
    _switch(rbac_graph, world, grant)
    # Then one registered event names the switched session, with metadata only.
    with psycopg.connect(superuser_database_url) as database:
        rows = database.execute(
            "SELECT affected_record_id, payload FROM clinic_app.audit_event "
            "WHERE event_type = 'identity.delegate.session_switched'"
        ).fetchall()
    assert [record for record, _payload in rows] == [str(world.session_id)]
    payload = rows[0][1]
    assert set(payload) <= PAYLOAD_KEYS
    assert payload["clinic_id"] == str(rbac_graph.clinic_a)
    assert str(world.notes.enrollment_id) not in str(payload)
