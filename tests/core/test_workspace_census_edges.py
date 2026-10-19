"""Close inherited refusal edges in the reception lane's global census."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING
from unittest.mock import Mock
from uuid import uuid4

import pytest
from apps.ehr import history_views
from apps.ehr.history import read_history
from apps.identity.models import UserClinicRole
from apps.intake import views as intake_views
from apps.intake.patient_access import PATIENT_SESSION_KEY
from apps.intake.services import PatientAccessDeniedError, create_patient
from apps.tenancy.db import tenant_context
from django.test import Client
from django.urls import reverse

from core.test_navigation import _client_for, _get, _post, _reauthenticate
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from renewal.test_clinical_history import begin, save
from renewal.test_encounters import physician_client
from renewal.test_teleconsult_sessions import _create, seed, synthetic_provider

if TYPE_CHECKING:
    from uuid import UUID

    from apps.ehr.history import HistoryContext

    from rbac_fixtures import RbacGraph

__all__ = ["synthetic_provider"]
pytestmark = pytest.mark.django_db(transaction=True)


def test_clinical_bound_version_and_late_history_visibility_refusals(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, user = _client_for(rbac_graph, "physician")
    graph = replace(rbac_graph, physician=user.pk)
    encounter = begin(graph)
    with runtime_role(), tenant_context(user.pk, graph.organization_a):
        save(encounter)
        context = read_history(
            clinic_id=graph.clinic_a, encounter_id=encounter.pk, kind="problem"
        )
    assert len(context.entries) == 1
    response = _post(
        client,
        reverse("ehr:encounter", args=(graph.clinic_a,)),
        {
            "action": "review",
            "encounter_id": str(encounter.pk),
            "version_id": str(uuid4()),
        },
    )
    assert response.status_code == 403
    assert not response.cookies

    def hidden_entry(
        *, clinic_id: UUID, encounter_id: UUID, kind: str
    ) -> HistoryContext:
        # Keep the real audited read and simulate its later visibility fence.
        found = read_history(clinic_id=clinic_id, encounter_id=encounter_id, kind=kind)
        return replace(found, entries=[])

    recheck = Mock(wraps=hidden_entry)
    monkeypatch.setattr(history_views, "read_history", recheck)
    response = _post(
        client,
        reverse("ehr:history", args=(graph.clinic_a,)),
        {
            "action": "edit",
            "encounter_id": str(encounter.pk),
            "kind": "problem",
            "entry_id": str(context.entries[0].entry_id),
        },
    )
    recheck.assert_called_once()
    assert response.status_code == 403
    assert not response.cookies


def test_demographics_unknown_action_and_late_write_refusals(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    client, user = _client_for(graph, "receptionist")
    with runtime_role(), tenant_context(user.pk, graph.organization_a):
        registration = create_patient(
            clinic_id=graph.clinic_a,
            full_name="Synthetic census patient",
            birth_date=date(1990, 1, 1),
            idempotency_key=uuid4(),
        )
    url = reverse("intake:patient-demographics", args=(graph.clinic_a,))
    selected = {"enrollment_id": str(registration.enrollment.pk)}
    for fields in (
        {**selected, "action": "invalid"},
        {"enrollment_id": str(uuid4()), "action": "save"},
        {"enrollment_id": str(uuid4()), "action": "manage"},
    ):
        response = _post(client, url, fields)
        assert response.status_code == 404
        assert not response.cookies
    recheck = Mock(side_effect=PatientAccessDeniedError())
    monkeypatch.setattr(intake_views, "update_demographics", recheck)
    response = _post(
        client,
        url,
        {
            **selected,
            "action": "save",
            "expected_version": "1",
            "legal_name": "Synthetic corrected patient",
            "birth_date": "1990-01-01",
            "reason": "Synthetic correction",
        },
    )
    recheck.assert_called_once()
    assert response.status_code == 404
    assert not response.cookies


def test_teleconsult_json_conflicts_keep_real_session_authority(
    rbac_graph: RbacGraph, synthetic_provider: object
) -> None:
    graph = rbac_graph
    _, encounter, _, patient_id, _ = seed(graph)
    video = _create(graph, encounter)
    headers = {"Accept": "application/json"}
    with physician_client(graph) as staff:
        response = staff.post(
            reverse("teleconsult:staff", args=(graph.clinic_a,)),
            {"action": "start", "session_id": str(video.pk)},
            headers=headers,
        )
        assert response.status_code == 409
        assert response.json()["code"]
        assert not response.cookies
    patient = Client()
    session = patient.session
    session[PATIENT_SESSION_KEY] = str(patient_id)
    session.save()
    with runtime_role():
        response = patient.post(
            "/patient/teleconsult/",
            {"action": "join", "session_id": str(video.pk)},
            headers=headers,
        )
    assert response.status_code == 409
    assert response.json()["code"]
    assert not response.cookies


def test_invalid_workflow_start_renders_without_committing_a_command(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    client, user = _client_for(graph, "clinic_admin")
    with owner_context(graph.organization_a):
        UserClinicRole.objects.filter(user_id=user.pk, clinic_id=graph.clinic_a).update(
            workflow_bundle_version=2
        )
    _reauthenticate(client, user.pk, "clinic_admin")
    url = reverse("workflows:runs", args=(graph.clinic_a,))
    assert _get(client, url).status_code == 200
    response = _post(client, url, {"action": "start"})
    assert response.status_code == 400
    assert not response.cookies
