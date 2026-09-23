"""Encounter acceptance using real PostgreSQL policies and the clinic_app role."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.audit.services import record_phase1_event
from apps.ehr import services, views
from apps.ehr.models import (
    ClinicalDocumentVersion,
    Encounter,
    EncounterIntakeReference,
    SpecialtyTemplate,
)
from apps.ehr.services import (
    SOAP_FIELDS,
    ClinicalAccessDeniedError,
    ClinicalConflictError,
    create_draft,
    open_encounter,
    publish_template,
    record_clinical_note,
    view_version,
)
from apps.identity.models import User, UserClinicRole
from apps.intake.models import PatientClinicEnrollment, QuestionnaireEvent
from apps.intake.patient_access import patient_session_context, redeem_invitation
from apps.intake.questionnaires import (
    assign_questionnaire,
    reopen_response,
    submit_intake,
)
from apps.intake.questionnaires import publish_template as publish_questionnaire
from apps.intake.services import issue_invitation
from apps.scheduling.services import cancel_appointment
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, connections, transaction
from django.test import Client

from appointment_service_support import (
    AppointmentSetup,
    create_synthetic_appointment,
    seed_appointment_setup,
)
from otp_test_support import create_totp_device, fixed_otp_time, token_for
from otp_test_support import runtime_role as http_runtime_role
from patient_service_support import runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from apps.scheduling.models import Appointment

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
CONTENT: dict[str, str] = dict(
    zip(
        SOAP_FIELDS,
        (
            "Relato sintético",
            "Exame sintético",
            "Avaliação sintética",
            "Plano sintético",
        ),
        strict=True,
    )
)
TABLES = {
    f"ehr_{name}"
    for name in (
        "encounter",
        "specialtytemplate",
        "clinicaldocument",
        "clinicaldocumentversion",
        "encounterintakereference",
    )
}


@contextmanager
def setup_context(organization_id: UUID) -> Iterator[None]:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)", [str(organization_id)]
        )
        yield


def seed(graph: RbacGraph) -> tuple[Appointment, SpecialtyTemplate]:
    setup = seed_appointment_setup(graph)
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        appointment = create_synthetic_appointment(setup)
    with setup_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=graph.clinic_admin,
            role="clinic_admin",
        )
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        template = publish_template(
            clinic_id=graph.clinic_a,
            key="geral",
            title="Clínica geral",
            prompts=dict.fromkeys(SOAP_FIELDS, "Registro do médico"),
        )
    return appointment, template


def draft(
    graph: RbacGraph, appointment: Appointment, template: SpecialtyTemplate
) -> ClinicalDocumentVersion:
    encounter = open_encounter(clinic_id=graph.clinic_a, appointment_id=appointment.pk)
    return create_draft(
        clinic_id=graph.clinic_a, encounter_id=encounter.pk, template_id=template.pk
    )


def test_exact_draft_template_author_revision_and_atomic_audit(
    rbac_graph: RbacGraph,
) -> None:
    appointment, template = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        version = draft(rbac_graph, appointment, template)
        saved = record_clinical_note(
            clinic_id=rbac_graph.clinic_a,
            version_id=version.pk,
            expected_revision=1,
            content=CONTENT,
        )
        assert saved.revision == 2
        assert saved.author_id == rbac_graph.physician
        assert saved.created_at == version.created_at
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        newer = publish_template(
            clinic_id=rbac_graph.clinic_a,
            key="geral",
            title="Nova versão",
            prompts=dict.fromkeys(SOAP_FIELDS, "Novo"),
        )
        assert newer.version == 2
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        resumed = draft(rbac_graph, appointment, newer)
        viewed = view_version(clinic_id=rbac_graph.clinic_a, version_id=resumed.pk)
        assert viewed.pk == version.pk
        assert viewed.template_id == template.pk
        assert viewed.subjective == CONTENT["subjective"]
        assert viewed.revision == 2
    with setup_context(rbac_graph.organization_a):
        events = list(
            AuditEvent.objects.filter(event_type__startswith="ehr.").values_list(
                "event_type", "payload"
            )
        )
        assert [e[0] for e in events].count("ehr.encounter.opened") == 1
        assert {e[0] for e in events} >= {
            "ehr.document.draft_created",
            "ehr.document.saved",
            "ehr.record.viewed",
        }
        assert all(
            set(payload) <= {"clinic_id", "object_verb", "reason_code"}
            for _, payload in events
        )
        assert all(CONTENT["subjective"] not in str(payload) for _, payload in events)


def test_parallel_starts_and_drafts_converge(rbac_graph: RbacGraph) -> None:
    appointment, template = seed(rbac_graph)
    barrier = Barrier(2, timeout=15)

    def start() -> tuple[UUID, UUID]:
        try:
            with (
                runtime_role(),
                tenant_context(rbac_graph.physician, rbac_graph.organization_a),
            ):
                barrier.wait()
                note = draft(rbac_graph, appointment, template)
                return note.document.encounter_id, note.pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: start(), range(2)))
    assert results[0] == results[1]
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        assert Encounter.objects.filter(appointment=appointment).count() == 1
        assert ClinicalDocumentVersion.objects.count() == 1


def test_unchanged_save_is_a_no_op(rbac_graph: RbacGraph) -> None:
    """Saving identical content leaves the stored envelope and revision alone."""
    appointment, template = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        version = draft(rbac_graph, appointment, template)
        saved = record_clinical_note(
            clinic_id=rbac_graph.clinic_a,
            version_id=version.pk,
            expected_revision=1,
            content=CONTENT,
        )
        assert saved.revision == 2
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT content FROM clinic_app.ehr_clinicaldocumentversion "
                "WHERE id = %s",
                [version.pk],
            )
            row = cursor.fetchone()
            assert row is not None
            envelope = bytes(row[0])
        for _ in range(2):
            again = record_clinical_note(
                clinic_id=rbac_graph.clinic_a,
                version_id=version.pk,
                expected_revision=2,
                content=dict(CONTENT),
            )
            assert again.revision == 2
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT content, revision FROM "
                "clinic_app.ehr_clinicaldocumentversion WHERE id = %s",
                [version.pk],
            )
            row = cursor.fetchone()
            assert row is not None
            assert bytes(row[0]) == envelope
            assert row[1] == 2
        changed = record_clinical_note(
            clinic_id=rbac_graph.clinic_a,
            version_id=version.pk,
            expected_revision=2,
            content={**CONTENT, "plan": "Plano sintético revisado"},
        )
        assert changed.revision == 3
    with setup_context(rbac_graph.organization_a):
        assert AuditEvent.objects.filter(event_type="ehr.document.saved").count() == 2


def test_stale_and_failed_save_preserve_prior_data(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    appointment, template = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        version = draft(rbac_graph, appointment, template)
        record_clinical_note(
            clinic_id=rbac_graph.clinic_a,
            version_id=version.pk,
            expected_revision=1,
            content=CONTENT,
        )
        with pytest.raises(ClinicalConflictError, match="stale_revision"):
            record_clinical_note(
                clinic_id=rbac_graph.clinic_a,
                version_id=version.pk,
                expected_revision=1,
                content=dict.fromkeys(SOAP_FIELDS, "stale"),
            )
        original = record_phase1_event

        def fail_save_audit(event_type: str, **kwargs: UUID) -> int:
            if event_type == "ehr.document.saved":
                message = "synthetic audit storage failure"
                raise DatabaseError(message)
            return original(event_type, **kwargs)

        monkeypatch.setattr(services, "record_phase1_event", fail_save_audit)
        with pytest.raises(DatabaseError, match="synthetic"):
            record_clinical_note(
                clinic_id=rbac_graph.clinic_a,
                version_id=version.pk,
                expected_revision=2,
                content=dict.fromkeys(SOAP_FIELDS, "must roll back"),
            )
        stored = view_version(clinic_id=rbac_graph.clinic_a, version_id=version.pk)
        assert stored.subjective == CONTENT["subjective"]
        assert stored.revision == 2


@pytest.mark.parametrize("actor", ["shared_user", "clinic_admin"])
def test_reception_and_admin_denied_by_service_and_raw_rls(
    rbac_graph: RbacGraph, actor: str
) -> None:
    appointment, template = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        version = draft(rbac_graph, appointment, template)
    with (
        runtime_role(),
        tenant_context(getattr(rbac_graph, actor), rbac_graph.organization_a),
    ):
        assert not ClinicalDocumentVersion.objects.filter(pk=version.pk).exists()
        with pytest.raises(ClinicalAccessDeniedError):
            view_version(clinic_id=rbac_graph.clinic_a, version_id=version.pk)
        with pytest.raises(ClinicalAccessDeniedError):
            open_encounter(clinic_id=rbac_graph.clinic_a, appointment_id=appointment.pk)
        assert Encounter.objects.filter(appointment=appointment).exists()
    with setup_context(rbac_graph.organization_a):
        assert (
            AuditEvent.objects.filter(
                event_type="ehr.access.denied", payload__reason_code="role_denied"
            ).count()
            == 2
        )


def test_other_physician_cross_clinic_tenant_and_revocation_fail_closed(
    rbac_graph: RbacGraph,
) -> None:
    appointment, template = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        version = draft(rbac_graph, appointment, template)
    with setup_context(rbac_graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user_id=rbac_graph.clinic_admin,
            role="physician",
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        with pytest.raises(ClinicalAccessDeniedError):
            open_encounter(clinic_id=rbac_graph.clinic_a, appointment_id=appointment.pk)
        with pytest.raises(ClinicalAccessDeniedError):
            view_version(clinic_id=rbac_graph.clinic_a, version_id=version.pk)
        with pytest.raises(ClinicalAccessDeniedError):
            view_version(clinic_id=rbac_graph.clinic_b, version_id=version.pk)
        assert not ClinicalDocumentVersion.objects.exists()
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_b),
    ):
        assert not Encounter.objects.filter(appointment=appointment).exists()
        with pytest.raises(ClinicalAccessDeniedError):
            view_version(clinic_id=rbac_graph.clinic_a, version_id=version.pk)
    with setup_context(rbac_graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user_id=rbac_graph.physician,
            role="receptionist",
        )
        UserClinicRole.objects.filter(
            user_id=rbac_graph.physician, role="physician"
        ).delete()
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        assert not ClinicalDocumentVersion.objects.exists()
        with pytest.raises(ClinicalAccessDeniedError):
            record_clinical_note(
                clinic_id=rbac_graph.clinic_a,
                version_id=version.pk,
                expected_revision=1,
                content=CONTENT,
            )


def test_cancellation_retains_existing_record_but_blocks_new(
    rbac_graph: RbacGraph,
) -> None:
    appointment, template = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        version = draft(rbac_graph, appointment, template)
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        cancel_appointment(appointment_id=appointment.pk, reason="patient_request")
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        assert draft(rbac_graph, appointment, template).pk == version.pk
        record_clinical_note(
            clinic_id=rbac_graph.clinic_a,
            version_id=version.pk,
            expected_revision=1,
            content=CONTENT,
        )
    # A distinct cancelled appointment must not acquire an encounter.
    setup = seed_appointment_setup_for_existing(rbac_graph, appointment)
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        second = create_synthetic_appointment(
            setup, start_local="2035-06-02T10:00", end_local="2035-06-02T11:00"
        )
        cancel_appointment(appointment_id=second.pk, reason="patient_request")
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        with pytest.raises(ClinicalConflictError, match="precondition_failed"):
            open_encounter(clinic_id=rbac_graph.clinic_a, appointment_id=second.pk)
        assert not Encounter.objects.filter(appointment=second).exists()


def seed_appointment_setup_for_existing(
    graph: RbacGraph, appointment: Appointment
) -> AppointmentSetup:
    with setup_context(graph.organization_a):
        enrollment = PatientClinicEnrollment.objects.get(
            patient_id=appointment.patient_id, clinic_id=graph.clinic_a
        )
    return AppointmentSetup(
        graph.organization_a,
        graph.clinic_a,
        graph.shared_user,
        graph.physician,
        enrollment.pk,
        appointment.patient_id,
    )


def test_intake_reference_is_immutable_submission_not_reopened_answers(
    rbac_graph: RbacGraph,
) -> None:
    appointment, template = seed(rbac_graph)
    setup = seed_appointment_setup_for_existing(rbac_graph, appointment)
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        invitation = issue_invitation(
            clinic_id=rbac_graph.clinic_a, enrollment_id=setup.enrollment_id
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        questionnaire = publish_questionnaire(
            clinic_id=rbac_graph.clinic_a,
            key="intake",
            title="Pré-consulta",
            questions=[
                {
                    "id": "q_text",
                    "label": "Relato",
                    "type": "text",
                    "required": False,
                    "max_length": 40,
                    "options": [],
                }
            ],
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        response = assign_questionnaire(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=setup.enrollment_id,
            template_id=questionnaire.pk,
            appointment_id=appointment.pk,
        )
    with runtime_role():
        session = redeem_invitation(rbac_graph.clinic_a, invitation.secret)
    assert session is not None
    with runtime_role(), patient_session_context(session):
        submit_intake(
            response_id=response.pk,
            expected_revision=1,
            answers={"q_text": "Envio original"},
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        version = draft(rbac_graph, appointment, template)
        reference = EncounterIntakeReference.objects.get(
            encounter=version.document.encounter
        )
        assert reference.submission.revision == 2
        reopen_response(
            clinic_id=rbac_graph.clinic_a,
            response_id=response.pk,
            expected_revision=2,
            reason="Correção",
        )
    with runtime_role(), patient_session_context(session):
        submit_intake(
            response_id=response.pk,
            expected_revision=3,
            answers={"q_text": "Envio corrigido"},
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        assert draft(rbac_graph, appointment, template).pk == version.pk
        assert EncounterIntakeReference.objects.get(
            pk=reference.pk
        ).submission.answers == {"q_text": "Envio original"}
        assert (
            QuestionnaireEvent.objects.filter(
                response=response, action="submitted"
            ).count()
            == 2
        )


@contextmanager
def physician_client(graph: RbacGraph) -> Iterator[Client]:
    username = User.objects.get(pk=graph.physician).username
    device = create_totp_device(graph.physician, confirmed=True)
    client = Client()
    with http_runtime_role(), fixed_otp_time():
        assert (
            client.post(
                "/auth/login/", {"username": username, "password": RBAC_RAW_CREDENTIAL}
            ).status_code
            == 302
        )
        assert (
            client.post(
                "/auth/verify/",
                {"otp_device": device.persistent_id, "otp_token": token_for(device)},
            ).status_code
            == 302
        )
        yield client


def test_http_explicit_save_reload_stale_invalid_and_failure(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    appointment, template = seed(rbac_graph)
    url = f"/ehr/clinics/{rbac_graph.clinic_a}/encounter/"
    with physician_client(rbac_graph) as client:
        empty = client.get(url)
        assert empty.status_code == 200
        assert "no-store" in empty.headers["Cache-Control"]
        assert client.post(url, {"action": "invalid"}).status_code == 403
        assert (
            client.post(
                url, {"action": "open", "appointment_id": "invalid"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                url, {"action": "open", "appointment_id": appointment.pk}
            ).status_code
            == 302
        )
        assert client.get(url).context["templates"].count() == 1
        assert (
            client.post(
                url, {"action": "template", "template_id": template.pk}
            ).status_code
            == 302
        )
        page = client.get(url)
        version = page.context["version"]
        payload = {"action": "save", "version_id": version.pk, "revision": 1, **CONTENT}
        invalid = client.post(url, {**payload, "objective": "x" * 20001})
        assert invalid.status_code == 400
        assert invalid.context["form"].data["objective"] == "x" * 20001
        assert client.post(url, payload).status_code == 302
        assert client.get(url).context["version"].revision == 2
        stale = client.post(url, {**payload, "subjective": "Unsaved old edit"})
        assert stale.status_code == 409
        assert stale.context["form"].data["subjective"] == "Unsaved old edit"
        assert stale.context["form"].data["revision"] == "1"
        assert client.get(url).context["version"].subjective == CONTENT["subjective"]

        def fail(**kwargs: object) -> ClinicalDocumentVersion:
            message = "synthetic storage failure"
            raise DatabaseError(message)

        monkeypatch.setattr(views, "record_clinical_note", fail)
        failed = client.post(
            url, {**payload, "revision": 2, "subjective": "Unsaved retry"}
        )
        assert failed.status_code == 503
        assert failed.context["unsaved"] is True
        assert failed.context["form"].data["subjective"] == "Unsaved retry"
        assert client.get(url).context["version"].revision == 2
        assert client.post(url, {"action": "invalid"}).status_code == 403


def test_exact_rls_acl_and_immutable_template(rbac_graph: RbacGraph) -> None:
    appointment, template = seed(rbac_graph)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname,relrowsecurity,relforcerowsecurity,"
            "relowner::regrole::text FROM pg_class WHERE relname = ANY(%s)",
            [list(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            (table, True, True, "clinic_owner") for table in TABLES
        }
        cursor.execute(
            "SELECT tablename,policyname FROM pg_policies "
            "WHERE schemaname='clinic_app' AND tablename=ANY(%s)",
            [list(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            (table, policy)
            for table in TABLES
            for policy in ("setup_tenant", "clinical_read", "clinical_insert")
        } | {
            ("ehr_encounter", "clinical_lock"),
            ("ehr_clinicaldocumentversion", "clinical_update"),
        }
        cursor.execute(
            "SELECT table_name,privilege_type "
            "FROM information_schema.role_table_grants "
            "WHERE grantee='clinic_app' AND table_schema='clinic_app' "
            "AND table_name=ANY(%s)",
            [list(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            (table, privilege) for table in TABLES for privilege in ("SELECT", "INSERT")
        }
        cursor.execute(
            "SELECT table_name,column_name FROM information_schema.role_column_grants "
            "WHERE grantee='clinic_app' AND privilege_type='UPDATE' "
            "AND table_schema='clinic_app' AND table_name=ANY(%s)",
            [list(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            ("ehr_encounter", field) for field in ("revision", "state", "closed_at")
        } | {
            ("ehr_clinicaldocumentversion", field)
            for field in (
                "content",
                "content_sha256",
                "amendment_reason",
                "revision",
                "updated_at",
                "state",
                "content_digest",
                "finalized_at",
            )
        }
    with (
        setup_context(rbac_graph.organization_a),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        SpecialtyTemplate.objects.filter(pk=template.pk).update(title="Replacement")
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        version = draft(rbac_graph, appointment, template)
        with pytest.raises(DatabaseError), transaction.atomic():
            ClinicalDocumentVersion.objects.filter(pk=version.pk).update(
                content_sha256="0" * 64
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            ClinicalDocumentVersion.objects.filter(pk=version.pk).update(
                template_id=uuid4()
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            ClinicalDocumentVersion.objects.filter(pk=version.pk).delete()
