"""Native malformed forms and failure boundaries complement the HTTP workflows."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import Mock
from uuid import uuid4

import pytest
from apps.core.api import views as api_views
from apps.ehr import attachment_views
from apps.ehr import views as ehr_views
from apps.ehr.services import ClinicalConflictError
from apps.scheduling.services import AgendaInputError
from django.urls import reverse

from core.test_navigation import _client_for, _post
from core.test_workspace_in_view_refusals import _send
from renewal.test_clinical_history import begin

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    ("route", "fields"),
    [
        ("intake:patient-contacts", {"action": "edit"}),
        ("intake:patient-contacts", {"action": "save", "channel": "invalid"}),
        ("intake:patient-contacts", {"action": "verify"}),
        ("intake:patient-contacts", {"action": "preference"}),
        ("intake:patient-access", {"action": "issue"}),
        ("intake:patient-access", {"action": "revoke"}),
        ("billing:charges", {"action": "create"}),
        ("scheduling:appointment-create", {"mode": "prepare"}),
    ],
)
def test_native_form_refusal_edges(
    rbac_graph: RbacGraph, route: str, fields: dict[str, str]
) -> None:
    client, _ = _client_for(rbac_graph, "receptionist")
    response = _post(
        client, reverse(route, kwargs={"clinic_id": rbac_graph.clinic_a}), fields
    )
    assert 400 <= response.status_code < 500


def test_ehr_unknown_selections_and_history_edit_edges(rbac_graph: RbacGraph) -> None:
    client, user = _client_for(rbac_graph, "physician")
    graph = replace(rbac_graph, physician=user.pk)
    encounter = begin(graph)
    for action in ("close", "review", "show"):
        response = _post(
            client,
            reverse("ehr:encounter", kwargs={"clinic_id": graph.clinic_a}),
            {
                "action": action,
                "encounter_id": str(uuid4()),
                "version_id": str(uuid4()),
            },
        )
        assert response.status_code == 403
    for fields in (
        {"action": "new", "kind": "invalid"},
        {"action": "edit", "kind": "allergy", "entry_id": str(uuid4())},
    ):
        response = _post(
            client,
            reverse("ehr:history", kwargs={"clinic_id": graph.clinic_a}),
            {**fields, "encounter_id": str(encounter.pk)},
        )
        assert response.status_code == 403


def test_translated_service_failures_are_real_http_refusals(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, user = _client_for(rbac_graph, "physician")
    graph = replace(rbac_graph, physician=user.pk)
    encounter = begin(graph)
    with monkeypatch.context() as patch:
        patch.setattr(
            attachment_views,
            "authorize_attachment_encounter",
            Mock(side_effect=ClinicalConflictError("precondition_failed")),
        )
        response = _post(
            client,
            reverse("ehr:attachments", kwargs={"clinic_id": graph.clinic_a}),
            {"action": "open", "encounter_id": str(encounter.pk)},
        )
        assert response.status_code == 403
    with monkeypatch.context() as patch:
        patch.setattr(
            ehr_views,
            "open_encounter",
            Mock(side_effect=ClinicalConflictError("precondition_failed")),
        )
        response = _post(
            client,
            reverse("ehr:encounter", kwargs={"clinic_id": graph.clinic_a}),
            {"action": "open", "appointment_id": str(uuid4())},
        )
        assert response.status_code == 409
    with monkeypatch.context() as patch:
        patch.setattr(api_views, "view_agenda", Mock(side_effect=AgendaInputError()))
        response = _send(
            client,
            reverse("ui_api:agenda-query"),
            "POST",
            {
                "clinic_id": str(graph.clinic_a),
                "view": "day",
                "date": "2033-05-18",
                "page": "1",
            },
            htmx=False,
        )
        assert response.status_code == 400
