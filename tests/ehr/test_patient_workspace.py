"""Native/HTMX chart entry, patient binding and side-effect-free refusal."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ehr import workspace_views
from apps.ehr.models import CopiedNoteBlock, MedicationStatement, Observation
from apps.identity.models import Clinic, User
from django.conf import settings
from django.urls import reverse
from django.utils.translation import gettext

from ehr.longitudinal_support import chart_world, finalized, second_draft
from ehr.test_addenda import signed_in
from ehr.test_refusal_matrix import snapshot
from identity.permission_support import owner_context, permission_actor
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from apps.ehr.timeline import TimelineFilters
    from django.http import HttpRequest

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("enhanced", [False, True])
def test_unconfigured_timezone_refuses_without_committed_side_effects(
    rbac_graph: RbacGraph,
    superuser_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    enhanced: bool,
) -> None:
    world = chart_world(rbac_graph)
    url = reverse("ehr:patient-workspace", kwargs={"clinic_id": rbac_graph.clinic_a})
    original = workspace_views._filters

    def unconfigured(request: HttpRequest, clinic: Clinic) -> TimelineFilters:
        # NOT NULL excludes this stored state. Inject only the defensive input;
        # the real filter, actor/care checks and request transaction still run.
        local = Clinic.from_db("default", ("id", "timezone"), (clinic.pk, None))
        return original(request, local)

    with signed_in(rbac_graph.physician) as client:
        opened = client.post(url, {"action": "open", "enrollment_id": world.enrollment})
        assert opened.status_code == 200
        before_session = dict(client.session)
        before = snapshot(superuser_database_url)
        monkeypatch.setattr(workspace_views, "_filters", unconfigured)
        for body in ({}, {"action": "open", "enrollment_id": world.enrollment}):
            response = client.post(
                url, body, HTTP_HX_REQUEST="true" if enhanced else "false"
            )
            assert response.status_code == 403
            assert dict(client.session) == before_session
            assert settings.SESSION_COOKIE_NAME not in response.cookies
            assert snapshot(superuser_database_url) == before


def test_native_workspace_records_vitals_and_reconciles_medication(
    rbac_graph: RbacGraph,
) -> None:
    world = chart_world(rbac_graph)
    url = reverse("ehr:patient-workspace", kwargs={"clinic_id": rbac_graph.clinic_a})
    with signed_in(rbac_graph.physician) as client:
        opened = client.post(url, {"action": "open", "enrollment_id": world.enrollment})
        assert opened.status_code == 200
        vitals = client.post(
            url,
            {
                "action": "vitals",
                "tab": "vitals",
                "enrollment_id": world.enrollment,
                "vitals-temperature": "37,5",
                "vitals-weight": "70,0",
                "vitals-height": "175",
                "vitals-provenance": "reported_by_patient",
            },
        )
        assert vitals.status_code == 200
        assert "37,5" in vitals.content.decode()
        assert "22,86" in vitals.content.decode()
        for tab in ("timeline", "encounters", "documents", "forms", "overview"):
            response = client.post(
                url,
                {"action": "tab", "tab": tab, "enrollment_id": world.enrollment},
                HTTP_HX_REQUEST="true",
            )
            assert response.status_code == 200
            assert b'id="patient-workspace"' in response.content
            assert b"<html" not in response.content
        medication = client.post(
            url,
            {
                "action": "medication",
                "tab": "medications",
                "enrollment_id": world.enrollment,
                "medication-text": "Medicamento sintético 28",
                "medication-dose_value": "5,0",
                "medication-dose_unit": "mg",
                "medication-route": "oral",
                "medication-times_per_day": "2",
                "medication-status": "unknown",
                "medication-provenance": "reported_by_patient",
            },
        )
        assert medication.status_code == 200
    with owner_context(rbac_graph.organization_a):
        rows = list(Observation.objects.filter(patient_id=world.patient))
        assert len(rows) == 4
        assert {row.provenance for row in rows} == {"reported_by_patient"}
        statement = MedicationStatement.objects.get(patient_id=world.patient)
    with signed_in(rbac_graph.physician) as client:
        confirmed = client.post(
            url,
            {
                "action": "reconcile",
                "tab": "medications",
                "enrollment_id": world.enrollment,
                "statement_id": statement.pk,
                "revision": 1,
                "status": "active",
                "reviewed": "yes",
            },
        )
        assert confirmed.status_code == 200
    with owner_context(rbac_graph.organization_a):
        statement.refresh_from_db()
        assert (statement.status, statement.origin, statement.provenance) == (
            "active",
            "reported_by_patient",
            "clinician_confirmed",
        )


@pytest.mark.parametrize("enhanced", [False, True])
def test_invalid_unit_keeps_input_and_does_not_select_or_write(
    rbac_graph: RbacGraph, enhanced: bool
) -> None:
    world = chart_world(rbac_graph)
    url = reverse("ehr:patient-workspace", kwargs={"clinic_id": rbac_graph.clinic_a})
    with signed_in(rbac_graph.physician) as client:
        before = dict(client.session)
        response = client.post(
            url,
            {
                "action": "observation",
                "tab": "vitals",
                "enrollment_id": world.enrollment,
                "observation-kind": "temperature",
                "observation-value": "37,5",
                "observation-unit": "kg",
                "observation-provenance": "observed",
            },
            HTTP_HX_REQUEST="true" if enhanced else "false",
        )
        assert response.status_code == 400
        assert (
            gettext("Choose a valid unit for this observation.")
            in response.content.decode()
        )
        assert b'value="37,5"' in response.content
        assert f"ehr.patient.{rbac_graph.clinic_a}" not in client.session
        assert f"workspace.patient.{rbac_graph.clinic_a}" not in client.session
        assert dict(client.session) == before
        assert settings.SESSION_COOKIE_NAME not in response.cookies
    with owner_context(rbac_graph.organization_a):
        assert not Observation.objects.exists()


def test_observation_scope_does_not_expose_narrative_or_future_tabs(
    rbac_graph: RbacGraph,
) -> None:
    actor, enrolled = permission_actor(rbac_graph, "nurse")
    nurse = User.objects.get(pk=actor)
    nurse.set_password(RBAC_RAW_CREDENTIAL)
    nurse.save(update_fields=("password",))
    url = reverse("ehr:patient-workspace", kwargs={"clinic_id": rbac_graph.clinic_a})
    with signed_in(actor) as client:
        response = client.post(url, {"action": "open", "enrollment_id": enrolled})
        assert response.status_code == 200
        assert b'value="medications"' not in response.content
        assert b'value="encounters"' not in response.content
        for tab in (
            "medications",
            "encounters",
            "documents",
            "results",
            "plans",
            "messages",
        ):
            denied = client.post(
                url, {"action": "tab", "enrollment_id": enrolled, "tab": tab}
            )
            assert denied.status_code == 403


def test_unknown_and_foreign_patient_selectors_have_identical_refusal(
    rbac_graph: RbacGraph,
) -> None:
    world = chart_world(rbac_graph)
    url = reverse("ehr:patient-workspace", kwargs={"clinic_id": rbac_graph.clinic_b})
    with signed_in(rbac_graph.physician) as client:
        replies = [
            client.post(url, {"action": "open", "enrollment_id": selector})
            for selector in (world.enrollment, uuid4())
        ]
        assert [reply.status_code for reply in replies] == [403, 403]
        assert replies[0].content == replies[1].content


@pytest.mark.parametrize("enhanced", [False, True])
def test_workspace_copies_and_reviews_the_exact_draft_revision(
    rbac_graph: RbacGraph, enhanced: bool
) -> None:
    world = chart_world(rbac_graph)
    source = finalized(rbac_graph, world.version)
    target = second_draft(rbac_graph, world, rbac_graph.physician)
    url = reverse("ehr:patient-workspace", kwargs={"clinic_id": rbac_graph.clinic_a})
    body = {"enrollment_id": world.enrollment, "tab": "encounters"}
    with signed_in(rbac_graph.physician) as client:
        opened = client.post(
            url,
            {**body, "action": "tab"},
            HTTP_HX_REQUEST="true" if enhanced else "false",
        )
        assert opened.status_code == 200
        assert b'value="copy"' in opened.content
        copied = client.post(
            url,
            {
                **body,
                "action": "copy",
                "source_version_id": source.pk,
                "target_version_id": target.pk,
                "section": "subjective",
                "revision": 1,
                "command_id": uuid4(),
            },
            HTTP_HX_REQUEST="true" if enhanced else "false",
        )
        assert copied.status_code == 200
    with owner_context(rbac_graph.organization_a):
        block = CopiedNoteBlock.objects.get(target=target)
        assert block.reviewed_at is None
        assert block.inserted_revision == 2
    with signed_in(rbac_graph.physician) as client:
        reviewed = client.post(
            url,
            {
                **body,
                "action": "review_copy",
                "block_id": block.pk,
                "revision": 2,
                "reviewed": "yes",
            },
            HTTP_HX_REQUEST="true" if enhanced else "false",
        )
        assert reviewed.status_code == 200
    with owner_context(rbac_graph.organization_a):
        block.refresh_from_db()
        assert block.reviewed_revision == 2
        assert block.reviewed_by_id == rbac_graph.physician
