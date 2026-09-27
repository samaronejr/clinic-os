"""Every delivered patient-bound route binds its authorized subject and revokes it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.core.patient_context import PATIENT_BOUND_VIEWS
from apps.intake.models import PatientClinicEnrollment
from apps.prescription.signing import initiate_signature
from apps.tenancy.db import tenant_context
from django.urls import reverse

from core.test_navigation import PATIENT, _client_for, _get, _post
from core.test_workspace_boundaries import remove_permission
from identity.permission_support import owner_context
from otp_test_support import runtime_role
from patient_http_support import seed_patients
from renewal.test_clinical_history import begin
from renewal.test_document_artifacts import seed_rendered
from renewal.test_encounters import physician_client
from renewal.test_prescribing_workflow import _provision
from renewal.test_questionnaires import seed as seed_questionnaire
from renewal.test_teleconsult_sessions import _create, _run_room, synthetic_provider
from renewal.test_teleconsult_sessions import seed as seed_video

if TYPE_CHECKING:
    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

__all__ = ("synthetic_provider",)
pytestmark = pytest.mark.django_db(transaction=True)


def _ehr_surface(
    graph: RbacGraph, view: str
) -> tuple[Client, str, dict[str, str], _MonkeyPatchedWSGIResponse]:
    encounter = begin(graph)
    url = reverse(view, kwargs={"clinic_id": graph.clinic_a})
    data = (
        {"action": "show", "encounter_id": str(encounter.pk)}
        if view == "ehr:encounter"
        else {"action": "open", "encounter_id": str(encounter.pk)}
    )
    with physician_client(graph) as client:
        response = client.post(url, data, follow=True)
    return client, url, data, response


def _prescription_surface(
    graph: RbacGraph, view: str, settings: SettingsWrapper
) -> tuple[Client, str, dict[str, str], _MonkeyPatchedWSGIResponse]:
    scope, document = seed_rendered(graph)
    kwargs = {"clinic_id": graph.clinic_a}
    data = {}
    if view == "prescription:draft":
        data = {"action": "open", "encounter_id": str(scope["encounter_id"])}
    elif view == "prescription:draft-encounter":
        kwargs["encounter_id"] = scope["encounter_id"]
    elif view == "prescription:review":
        kwargs["document_id"] = document.pk
    else:
        assert view == "prescription:signing"
        settings.PRESCRIPTION_SYNTHETIC_SIGNING = True
        _provision(graph)
        with runtime_role(), tenant_context(graph.physician, graph.organization_a):
            operation = initiate_signature(
                clinic_id=graph.clinic_a, document_id=document.pk
            )
        kwargs["operation_id"] = operation.pk
    url = reverse(view, kwargs=kwargs)
    with physician_client(graph) as client:
        response = client.post(url, data, follow=True) if data else client.get(url)
    return client, url, data, response


@pytest.mark.parametrize("view", sorted(PATIENT_BOUND_VIEWS))
def test_each_patient_surface_binds_and_refuses_revocation(
    rbac_graph: RbacGraph,
    view: str,
    settings: SettingsWrapper,
    synthetic_provider: object,
) -> None:
    graph = rbac_graph
    role = "physician"
    if view.startswith("ehr:"):
        client, url, data, response = _ehr_surface(graph, view)
    elif view.startswith("prescription:"):
        client, url, data, response = _prescription_surface(graph, view, settings)
    elif view == "intake:questionnaire-staff":
        form, _ = seed_questionnaire(graph)
        url = reverse(view, kwargs={"clinic_id": graph.clinic_a})
        data = {"action": "inspect", "response_id": str(form.pk)}
        with physician_client(graph) as client:
            response = client.post(url, data)
    elif view == "teleconsult:staff":
        _, encounter, _, _, _ = seed_video(graph)
        session = _create(graph, encounter)
        assert _run_room(graph, session) == "succeeded"
        url = reverse(view, kwargs={"clinic_id": graph.clinic_a})
        data = {"action": "join", "session_id": str(session.pk)}
        with physician_client(graph) as client:
            response = client.post(url, data)
    else:
        assert view in {"intake:patient-contacts", "intake:patient-access"}, (
            "Uncovered patient surface"
        )
        role = "receptionist"
        client, user = _client_for(graph, role)
        seed_patients(graph, user.pk, graph.clinic_a, (PATIENT,))
        with owner_context(graph.organization_a):
            enrollment = PatientClinicEnrollment.objects.get(clinic_id=graph.clinic_a)
        url = reverse(view, kwargs={"clinic_id": graph.clinic_a})
        data = {"action": "manage", "enrollment_id": str(enrollment.pk)}
        response = _post(client, url, data)
    assert response.status_code == 200
    assert b"data-patient-banner" in response.content, view
    key = f"workspace.patient.{graph.clinic_a}"
    assert key in client.session
    remove_permission(graph, role, "demographics.read")
    refused = _post(client, url, data) if data else _get(client, url)
    assert refused.status_code in {403, 404}
    assert b"data-patient-banner" not in refused.content
    assert key not in client.session
