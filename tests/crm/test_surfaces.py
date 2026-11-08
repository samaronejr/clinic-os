"""Native public and patient surfaces with body-only selection and optionality."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from unittest.mock import Mock
from uuid import uuid4

import pytest
from apps.crm import views as crm_views
from apps.crm.access import CrmAccessDeniedError, CrmRuleError
from apps.crm.models import Lead, SatisfactionSurvey
from apps.crm.patient import record_satisfaction, set_marketing_preference
from apps.crm.services import (
    conversion_report,
    convert_lead,
    create_lead,
    create_source,
    publish_booking_profile,
    redact_expired_leads,
)
from apps.identity.models import UserClinicRole
from apps.intake.patient_access import patient_session_context
from apps.scheduling.appointment_lifecycle import complete, start
from apps.scheduling.patient_booking import patient_slots
from apps.scheduling.services import AppointmentAccessDeniedError
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction
from django.test import Client
from django.utils.translation import gettext

from auth.stepup_test_support import create_role_actor
from crm.support import CrmWorld, world
from patient_service_support import runtime_role
from renewal.test_retention import staff_client
from renewal.test_self_booking import _client, _session
from scheduling.appointment_service_support import (
    arrived_synthetic_appointment,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def state(rbac_graph: RbacGraph, settings: SettingsWrapper) -> CrmWorld:
    return world(rbac_graph, settings)


def _profile(state: CrmWorld) -> None:
    with runtime_role(), tenant_context(state.manager, state.graph.organization_a):
        publish_booking_profile(
            clinic_id=state.graph.clinic_a,
            code="synthetic-clinic",
            description="Synthetic clinic appointment booking.",
        )


def test_prospect_is_separate_encrypted_and_conversion_is_explicit(
    state: CrmWorld,
) -> None:
    with runtime_role(), tenant_context(state.manager, state.graph.organization_a):
        source = create_source(
            clinic_id=state.graph.clinic_a, code="community", name="Synthetic source"
        )
        lead = create_lead(
            clinic_id=state.graph.clinic_a,
            name="Synthetic prospect",
            email="lead@example.invalid",
            source_id=source.pk,
        )
        assert lead.enrollment_id is None
        assert lead.state == "active"
        assert redact_expired_leads(clinic_id=state.graph.clinic_a) == 0
        convert_lead(
            clinic_id=state.graph.clinic_a,
            lead_id=lead.pk,
            enrollment_id=state.enrollment,
        )
        assert conversion_report(clinic_id=state.graph.clinic_a)["converted"] == 1
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT name,email FROM clinic_app.crm_lead WHERE id=%s", [lead.pk]
            )
            raw = cursor.fetchone()
        assert raw is not None
        assert b"Synthetic prospect" not in bytes(raw[0])
        assert b"lead@example.invalid" not in bytes(raw[1])
        with pytest.raises(CrmRuleError, match="lead_unavailable"):
            convert_lead(
                clinic_id=state.graph.clinic_a,
                lead_id=lead.pk,
                enrollment_id=state.enrollment,
            )


@pytest.mark.parametrize("value", ["", "<script>", "x" * 161])
def test_invalid_lead_values_are_refused(state: CrmWorld, value: str) -> None:
    with (
        runtime_role(),
        tenant_context(state.manager, state.graph.organization_a),
        pytest.raises(CrmRuleError),
    ):
        create_lead(clinic_id=state.graph.clinic_a, name=value, email="bad-address")


def test_cross_tenant_lead_and_source_are_indistinguishable(state: CrmWorld) -> None:
    with runtime_role(), tenant_context(state.manager, state.graph.organization_a):
        lead = create_lead(
            clinic_id=state.graph.clinic_a,
            name="Synthetic",
            email="lead@example.invalid",
        )
        for lead_id in (lead.pk, uuid4()):
            with pytest.raises(CrmAccessDeniedError):
                convert_lead(
                    clinic_id=state.graph.clinic_b,
                    lead_id=lead_id,
                    enrollment_id=state.enrollment,
                )
        assert not Lead.objects.filter(clinic_id=state.graph.clinic_c).exists()


def test_public_page_has_no_selector_in_url_and_requires_patient_verification(
    state: CrmWorld,
) -> None:
    _profile(state)
    client = Client()
    with runtime_role():
        assert client.get("/book/").status_code == 200
        selected = client.post(
            "/book/", {"action": "select", "clinic_code": "synthetic-clinic"}
        )
        assert selected.status_code == 302
        assert selected["Location"] == "/book/"
        page = client.get("/book/")
        assert page.status_code == 200
        assert gettext("Verify patient access").encode() in page.content
        assert "no-store" in page["Cache-Control"]
        assert "synthetic-clinic" not in page.content.decode()
        assert (
            client.post("/book/", {"action": "book", "slot": "forged"}).status_code
            == 403
        )
        assert client.get("/book/?patient=sentinel").status_code == 400
        assert client.post("/book/", {"action": "clear"}).status_code == 302
        assert (
            client.post(
                "/book/", {"action": "select", "clinic_code": "missing"}
            ).status_code
            == 404
        )


def test_public_booking_reuses_patient_slot_authority(rbac_graph: RbacGraph) -> None:
    setup = seed_appointment_setup(rbac_graph)
    manager = create_role_actor(rbac_graph, UserClinicRole.Role.CLINIC_MANAGER)
    with runtime_role(), tenant_context(manager.pk, rbac_graph.organization_a):
        publish_booking_profile(
            clinic_id=setup.clinic_id,
            code="verified-clinic",
            description="Synthetic clinic booking.",
        )
    session = _session(setup)
    client = _client(session)
    with runtime_role():
        client.post("/book/", {"action": "select", "clinic_code": "verified-clinic"})
        result = client.post("/book/", {"action": "slots", "day": "2035-06-02"})
        assert result.status_code == 200
        assert gettext("Book this time").encode() in result.content
    with runtime_role(), patient_session_context(session):
        slot = patient_slots(date(2035, 6, 2))[0]
    with runtime_role():
        result = client.post(
            "/book/",
            {
                "action": "book",
                "day": "2035-06-02",
                "slot": slot.token,
                "idempotency_key": str(uuid4()),
            },
        )
        assert result.status_code == 200
        assert (
            gettext("Booking recorded. Review it in your portal.").encode()
            in result.content
        )
        assert (
            client.post("/book/", {"action": "slots", "day": "invalid"}).status_code
            == 409
        )
        assert (
            client.post(
                "/book/", {"action": "slots", "enrollment_id": str(uuid4())}
            ).status_code
            == 403
        )


def test_opt_out_is_available_without_any_prior_consent(state: CrmWorld) -> None:
    client = _client(state.session)
    with runtime_role():
        page = client.get("/patient/outreach/")
        assert page.status_code == 200
        assert (
            gettext("No marketing channels are allowed by default.").encode()
            in page.content
        )
        result = client.post(
            "/patient/outreach/", {"choice": "out", "channel": "email"}
        )
        assert result.status_code == 200
        assert (
            client.post(
                "/patient/outreach/", {"choice": "bad", "channel": "email"}
            ).status_code
            == 409
        )
    with (
        runtime_role(),
        patient_session_context(state.session),
        pytest.raises(CrmRuleError),
    ):
        set_marketing_preference(channel="video", opted_in=True)


def test_manager_native_capture_preview_and_missing_consent_failure(
    state: CrmWorld,
) -> None:
    path = f"/clinics/{state.graph.clinic_a}/crm/"
    with staff_client(state.manager) as client:
        page = client.get(path)
        assert page.status_code == 200
        assert gettext("Ethical outreach").encode() in page.content
        assert (
            client.post(
                path,
                {"action": "source", "code": "community", "name": "Synthetic referral"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                path,
                {"action": "source", "code": "community", "name": "Duplicate source"},
            ).status_code
            == 409
        )
        captured = client.post(
            path,
            {
                "action": "lead",
                "name": "Synthetic prospect",
                "email": "prospect@example.invalid",
            },
        )
        assert captured.status_code == 200
        assert b"Synthetic prospect</option>" in captured.content
        assert b"Synthetic CRM Patient" in captured.content
        with tenant_context(state.manager, state.graph.organization_a):
            lead = Lead.objects.get(clinic_id=state.graph.clinic_a)
        converted = client.post(
            path,
            {
                "action": "convert",
                "lead_id": str(lead.pk),
                "enrollment_id": str(state.enrollment),
            },
        )
        assert converted.status_code == 200
        assert b"1 / 1" in converted.content
        assert (
            client.post(
                path,
                {
                    "action": "profile",
                    "code": "native-crm",
                    "description": "Synthetic clinic booking.",
                },
            ).status_code
            == 200
        )
        for _ in range(2):
            updated = client.post(
                path,
                {
                    "action": "profile",
                    "code": "native-crm",
                    "description": "Updated factual clinic introduction.",
                },
            )
            assert updated.status_code == 200
            assert b"Updated factual clinic introduction.</textarea>" in updated.content
        assert (
            client.post(
                path,
                {
                    "action": "profile",
                    "code": "changed-code",
                    "description": "Synthetic",
                },
            ).status_code
            == 409
        )
        assert (
            client.post(
                path,
                {
                    "action": "campaign",
                    "title": "Synthetic campaign",
                    "template_version_id": str(state.template),
                },
            ).status_code
            == 200
        )
        with (
            tenant_context(state.manager, state.graph.organization_a),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT id FROM clinic_app.crm_campaign WHERE clinic_id=%s",
                [state.graph.clinic_a],
            )
            row = cursor.fetchone()
        assert row is not None
        campaign = str(row[0])
        preview = client.post(path, {"action": "preview", "campaign_id": campaign})
        assert preview.status_code == 200
        assert b"data-excluded>1<" in preview.content
        assert (
            client.post(
                path,
                {
                    "action": "approve",
                    "campaign_id": campaign,
                    "expected_revision": "1",
                },
            ).status_code
            == 200
        )
        refused = client.post(
            path, {"action": "send", "campaign_id": campaign, "expected_revision": "2"}
        )
        assert refused.status_code == 409
        assert (
            client.post(
                path, {"action": "refresh", "campaign_id": campaign}
            ).status_code
            == 200
        )
        assert client.post(path, {"action": "unknown"}).status_code == 409


def test_public_invitation_uses_fixed_url_and_generic_failures(state: CrmWorld) -> None:
    _profile(state)
    client = Client(enforce_csrf_checks=True)
    with runtime_role():
        assert (
            client.post(
                "/book/", {"action": "select", "clinic_code": "synthetic-clinic"}
            ).status_code
            == 403
        )
    client = Client()
    with runtime_role():
        assert client.get("/book/access/").status_code == 302
        client.post("/book/", {"action": "select", "clinic_code": "synthetic-clinic"})
        page = client.get("/book/access/")
        assert page.status_code == 200
        assert b'type="password"' in page.content
        invalid = client.post("/book/access/", {"code": "synthetic-invalid"})
        assert invalid.status_code == 200
        assert b"synthetic-invalid" not in invalid.content
        assert (
            gettext("Access unavailable. Ask the clinic for a new invitation.").encode()
            in invalid.content
        )


def test_post_visit_score_only_for_own_completed_appointment(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    session = _session(setup)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment = arrived_synthetic_appointment(setup)
    with (
        runtime_role(),
        patient_session_context(session),
        pytest.raises(CrmRuleError, match="completed_visit_required"),
    ):
        record_satisfaction(appointment_id=appointment.pk, score=9)
    with runtime_role(), tenant_context(setup.practitioner_id, setup.organization_id):
        started = start(
            clinic_id=setup.clinic_id,
            appointment_id=appointment.pk,
            expected_revision=appointment.revision,
            command_id=uuid4(),
        )
        complete(
            clinic_id=setup.clinic_id,
            appointment_id=appointment.pk,
            expected_revision=started.revision,
            command_id=uuid4(),
        )
    with runtime_role(), patient_session_context(session):
        survey = record_satisfaction(appointment_id=appointment.pk, score=9)
        assert (
            record_satisfaction(appointment_id=appointment.pk, score=9).pk == survey.pk
        )
        with pytest.raises(CrmRuleError, match="score_already_recorded"):
            record_satisfaction(appointment_id=appointment.pk, score=0)
        with pytest.raises(CrmRuleError, match="invalid_score"):
            record_satisfaction(appointment_id=appointment.pk, score=True)
        assert SatisfactionSurvey.objects.get(pk=survey.pk).score == 9
        with transaction.atomic(), pytest.raises(DatabaseError):
            SatisfactionSurvey.objects.filter(pk=survey.pk).update(score=1)
    client = _client(session)
    with runtime_role():
        assert client.get("/patient/feedback/").status_code == 200
        assert (
            client.post(
                "/patient/feedback/",
                {"appointment_id": str(appointment.pk), "score": "9"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/patient/feedback/", {"appointment_id": "bad", "score": "-1"}
            ).status_code
            == 409
        )


def test_feedback_rechecks_patient_authority_inside_the_view(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(_session(seed_appointment_setup(rbac_graph)))
    with runtime_role():
        assert client.get("/patient/feedback/").status_code == 200
        # Booking authority can lapse after the middleware's initial session touch.
        monkeypatch.setattr(
            crm_views,
            "require_patient_booking_scope",
            Mock(side_effect=AppointmentAccessDeniedError),
        )
        assert client.get("/patient/feedback/").status_code == 403
