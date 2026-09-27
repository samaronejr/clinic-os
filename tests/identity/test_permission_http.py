"""Real HTTP denial shape and legacy receptionist booking acceptance."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ehr.services import ClinicalAccessDeniedError, open_encounter, view_version
from apps.identity.models import UserClinicRole
from apps.tenancy.db import tenant_context
from django.test import Client

from identity.permission_support import owner_context
from identity.test_identity_rbac import _seed_session
from patient_service_support import runtime_role
from renewal.test_encounters import draft, seed

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("role", ["receptionist", "finance"])
def test_reception_books_but_finance_and_reception_cannot_open_soap(
    rbac_graph: RbacGraph,
    role: str,
) -> None:
    # seed() books through create_appointment as the real receptionist, not ORM.
    appointment, template = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        version = draft(rbac_graph, appointment, template)
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.filter(
            user_id=rbac_graph.shared_user,
            clinic_id=rbac_graph.clinic_a,
        ).update(role=role)
    client = Client()
    _seed_session(client, rbac_graph, rbac_graph.shared_user)
    url = f"/ehr/clinics/{rbac_graph.clinic_a}/encounter/"
    with runtime_role():
        known = client.post(
            url, {"action": "open", "appointment_id": str(appointment.pk)}
        )
        unknown = client.post(url, {"action": "open", "appointment_id": str(uuid4())})
        soap = client.post(
            url, {"action": "show", "encounter_id": str(version.document.encounter_id)}
        )
        no_soap = client.post(url, {"action": "show", "encounter_id": str(uuid4())})
    assert (
        known.status_code
        == unknown.status_code
        == soap.status_code
        == no_soap.status_code
        == 403
    )
    assert known.content == unknown.content == soap.content == no_soap.content
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        for record in (version.pk, uuid4()):
            with pytest.raises(ClinicalAccessDeniedError):
                view_version(clinic_id=rbac_graph.clinic_a, version_id=record)
        for record in (appointment.pk, uuid4()):
            with pytest.raises(ClinicalAccessDeniedError):
                open_encounter(clinic_id=rbac_graph.clinic_a, appointment_id=record)
