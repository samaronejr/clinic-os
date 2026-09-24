"""Scoped teleconsult sessions under real FORCE RLS and the clinic_app role."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.comms.models import IntegrationOperation
from apps.comms.tasks import execute_operation as execute_operation_task
from apps.consent.services import (
    publish_text,
    revoke_consent,
)
from apps.core.integration import register_send_adapter
from apps.ehr.services import open_encounter
from apps.identity.models import UserClinicRole
from apps.intake.access import PatientAccessDeniedError
from apps.intake.models import PatientClinicEnrollment
from apps.intake.patient_access import (
    issue_invitation,
    patient_session_context,
    redeem_invitation,
)
from apps.intake.services import create_patient
from apps.scheduling.services import AppointmentLocalRange, create_appointment
from apps.teleconsult.adapters import PROVIDER, SyntheticRoomAdapter
from apps.teleconsult.models import (
    TeleconsultCredential,
    TeleconsultEvent,
    TeleconsultRoom,
    TeleconsultSession,
)
from apps.teleconsult.services import (
    RoomEntry,
    TeleconsultAccessDeniedError,
    TeleconsultConflictError,
    create_session,
    derived_state,
    end_consultation,
    enter_room,
    refresh_session,
    request_patient_join,
    request_physician_join,
    start_consultation,
)
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction
from django.utils import timezone
from psycopg import sql

from auth.stepup_test_support import create_role_actor
from patient_service_support import runtime_role
from renewal.test_consent import accept as accept_text
from renewal.test_encounters import seed as clinical_seed
from renewal.test_encounters import setup_context
from renewal.test_retention import admin

if TYPE_CHECKING:
    from uuid import UUID

    from apps.consent.models import ConsentAcceptance
    from apps.ehr.models import Encounter
    from apps.scheduling.models import Appointment
    from pytest_django.fixtures import SettingsWrapper

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
TEXT = "Texto sintético de teleconsulta.\n\nSem gravação ou transcrição."
PURPOSE = "teleconsultation"


@pytest.fixture
def synthetic_provider(settings: SettingsWrapper) -> SyntheticRoomAdapter:
    """Enable the synthetic room gate and re-register the boundary adapter."""
    settings.TELECONSULT_SYNTHETIC_PROVIDER = True
    adapter = SyntheticRoomAdapter()
    register_send_adapter(adapter)
    return adapter


def seed(
    graph: RbacGraph,
) -> tuple[Appointment, Encounter, ConsentAcceptance, UUID, UUID]:
    """Open one encounter and record one exact teleconsultation consent."""
    appointment, _template = clinical_seed(graph)
    manager = admin(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        encounter = open_encounter(
            clinic_id=graph.clinic_a, appointment_id=appointment.pk
        )
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        text = publish_text(clinic_id=graph.clinic_a, purpose=PURPOSE, text=TEXT)
        enrollment = PatientClinicEnrollment.objects.get(
            clinic_id=graph.clinic_a, patient_id=appointment.patient_id
        )
        invitation = issue_invitation(
            clinic_id=graph.clinic_a, enrollment_id=enrollment.pk
        )
    with runtime_role():
        patient_session = redeem_invitation(graph.clinic_a, invitation.secret)
    assert patient_session is not None
    with runtime_role(), patient_session_context(patient_session):
        acceptance = accept_text(text)
    return appointment, encounter, acceptance, patient_session, manager.pk


def _create(graph: RbacGraph, encounter: Encounter) -> TeleconsultSession:
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        return create_session(clinic_id=graph.clinic_a, encounter_id=encounter.pk)


def _operation(graph: RbacGraph, session: TeleconsultSession) -> IntegrationOperation:
    with setup_context(graph.organization_a):
        room = TeleconsultRoom.objects.get(session_id=session.pk)
        return IntegrationOperation.objects.get(pk=room.operation_id)


def _run_room(graph: RbacGraph, session: TeleconsultSession) -> str:
    operation_id = _operation(graph, session).pk
    with runtime_role():
        result = execute_operation_task.apply(
            kwargs={"operation_id": str(operation_id)}
        )
    return str(result.result)


def _physician_join(graph: RbacGraph, session: TeleconsultSession) -> str:
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        return request_physician_join(
            clinic_id=graph.clinic_a, session_id=session.pk
        ).token


def _patient_join(patient_session: UUID, session: TeleconsultSession) -> str:
    with runtime_role(), patient_session_context(patient_session):
        return request_patient_join(session_id=session.pk).token


def _physician_enter(graph: RbacGraph, token: str) -> RoomEntry:
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        return enter_room(token=token, role="physician")


def _patient_enter(patient_session: UUID, token: str) -> RoomEntry:
    with runtime_role(), patient_session_context(patient_session):
        return enter_room(token=token, role="patient")


def _kinds(graph: RbacGraph, session: TeleconsultSession) -> list[str]:
    with setup_context(graph.organization_a):
        return list(
            TeleconsultEvent.objects.filter(session_id=session.pk)
            .order_by("created_at", "pk")
            .values_list("kind", flat=True)
        )


def _audit_verbs(graph: RbacGraph, record_id: UUID) -> list[str]:
    with setup_context(graph.organization_a):
        rows = AuditEvent.objects.filter(
            organization_id=graph.organization_a,
            affected_record_id=str(record_id),
        ).values_list("payload__object_verb", flat=True)
    return sorted(rows)


def test_scoped_lifecycle_room_and_event_history(
    rbac_graph: RbacGraph, synthetic_provider: SyntheticRoomAdapter
) -> None:
    graph = rbac_graph
    _, encounter, _, patient_session, _ = seed(graph)
    session = _create(graph, encounter)
    assert session.state == "waiting"
    assert session.physician_id == graph.physician
    operation = _operation(graph, session)
    assert operation.channel == "video"
    assert operation.provider == PROVIDER
    assert operation.subject_type == "teleconsult.session"
    assert operation.subject_id == session.pk
    assert operation.status == "pending"
    # Creation converges on the stored session; no second room operation.
    assert _create(graph, encounter).pk == session.pk
    with setup_context(graph.organization_a):
        assert TeleconsultSession.objects.count() == 1
        assert TeleconsultRoom.objects.count() == 1
        assert IntegrationOperation.objects.filter(subject_id=session.pk).count() == 1
    # No credential and no start before the provider room exists.
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(TeleconsultConflictError) as not_ready,
    ):
        request_physician_join(clinic_id=graph.clinic_a, session_id=session.pk)
    assert not_ready.value.reason_code == "room_not_ready"
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(TeleconsultConflictError),
    ):
        start_consultation(clinic_id=graph.clinic_a, session_id=session.pk)
    assert _run_room(graph, session) == "succeeded"
    operation = _operation(graph, session)
    assert operation.status == "succeeded"
    assert operation.provider_reference == f"synthetic:room:tc-{session.pk}"
    physician_token = _physician_join(graph, session)
    patient_token = _patient_join(patient_session, session)
    assert physician_token != patient_token
    physician_entry = _physician_enter(graph, physician_token)
    patient_entry = _patient_enter(patient_session, patient_token)
    assert physician_entry.room_name == f"tc-{session.pk}"
    assert patient_entry.room_name == physician_entry.room_name
    assert physician_entry.role == "physician"
    assert patient_entry.role == "patient"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        started = start_consultation(clinic_id=graph.clinic_a, session_id=session.pk)
        assert started.state == "active"
        assert (
            start_consultation(clinic_id=graph.clinic_a, session_id=session.pk).revision
            == started.revision
        )
        ended = end_consultation(clinic_id=graph.clinic_a, session_id=session.pk)
        assert ended.state == "ended"
        assert (
            end_consultation(clinic_id=graph.clinic_a, session_id=session.pk).pk
            == ended.pk
        )
    # Ended credentials can never reopen the room.
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(TeleconsultConflictError) as closed,
    ):
        enter_room(token=physician_token, role="physician")
    assert closed.value.reason_code == "session_closed"
    with (
        runtime_role(),
        patient_session_context(patient_session),
        pytest.raises(TeleconsultConflictError),
    ):
        enter_room(token=patient_token, role="patient")
    with setup_context(graph.organization_a):
        assert not TeleconsultCredential.objects.filter(
            session_id=session.pk, revoked_at__isnull=True
        ).exists()
    assert _kinds(graph, session) == [
        "created",
        "joined",
        "joined",
        "started",
        "ended",
    ]
    assert _audit_verbs(graph, session.pk) == ["created", "ended", "started"]


def test_consent_required_then_revocation_cancels_room_and_join(
    rbac_graph: RbacGraph, synthetic_provider: SyntheticRoomAdapter
) -> None:
    graph = rbac_graph
    appointment, _template = clinical_seed(graph)
    manager = admin(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        encounter = open_encounter(
            clinic_id=graph.clinic_a, appointment_id=appointment.pk
        )
        with pytest.raises(TeleconsultConflictError) as missing:
            create_session(clinic_id=graph.clinic_a, encounter_id=encounter.pk)
    assert missing.value.reason_code == "consent_required"
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        text = publish_text(clinic_id=graph.clinic_a, purpose=PURPOSE, text=TEXT)
        enrollment = PatientClinicEnrollment.objects.get(
            clinic_id=graph.clinic_a, patient_id=appointment.patient_id
        )
        invitation = issue_invitation(
            clinic_id=graph.clinic_a, enrollment_id=enrollment.pk
        )
    with runtime_role():
        patient_session = redeem_invitation(graph.clinic_a, invitation.secret)
    assert patient_session is not None
    with runtime_role(), patient_session_context(patient_session):
        acceptance = accept_text(text)
    session = _create(graph, encounter)
    with runtime_role(), patient_session_context(patient_session):
        revoke_consent(acceptance_id=acceptance.pk)
    # The send-time recheck cancels the room operation with no external effect.
    assert _run_room(graph, session) == "cancelled"
    operation = _operation(graph, session)
    assert operation.status == "cancelled"
    assert operation.last_error == "subject_ineligible"
    assert operation.provider_reference is None
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        session = refresh_session(TeleconsultSession.objects.get(pk=session.pk))
        assert session.state == "failed"
        assert session.failure_reason == "consent_revoked"
        with pytest.raises(TeleconsultConflictError) as closed:
            request_physician_join(clinic_id=graph.clinic_a, session_id=session.pk)
        assert closed.value.reason_code == "session_closed"
    with (
        runtime_role(),
        patient_session_context(patient_session),
        pytest.raises(TeleconsultConflictError),
    ):
        request_patient_join(session_id=session.pk)
    assert _kinds(graph, session) == ["created", "failed"]


def test_room_creation_failure_marks_session_failed(
    rbac_graph: RbacGraph,
    synthetic_provider: SyntheticRoomAdapter,
    settings: SettingsWrapper,
) -> None:
    settings.TELECONSULT_SYNTHETIC_FAIL = True
    graph = rbac_graph
    _, encounter, _, patient_session, _ = seed(graph)
    session = _create(graph, encounter)
    assert _run_room(graph, session) == "retry"
    assert _run_room(graph, session) == "retry"
    assert _run_room(graph, session) == "retry"
    assert _run_room(graph, session) == "failed"
    operation = _operation(graph, session)
    assert operation.status == "failed"
    assert operation.provider_reference is None
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        session = refresh_session(TeleconsultSession.objects.get(pk=session.pk))
        assert session.state == "failed"
        assert session.failure_reason == "room_unavailable"
        with pytest.raises(TeleconsultConflictError):
            start_consultation(clinic_id=graph.clinic_a, session_id=session.pk)
    with (
        runtime_role(),
        patient_session_context(patient_session),
        pytest.raises(TeleconsultConflictError),
    ):
        request_patient_join(session_id=session.pk)
    assert _kinds(graph, session) == ["created", "failed"]


def test_foreign_principals_and_token_replays_fail_closed(
    rbac_graph: RbacGraph, synthetic_provider: SyntheticRoomAdapter
) -> None:
    graph = rbac_graph
    _, encounter, _, patient_session, _ = seed(graph)
    session = _create(graph, encounter)
    assert _run_room(graph, session) == "succeeded"
    other_physician = create_role_actor(graph, UserClinicRole.Role.PHYSICIAN)
    # A same-clinic physician who is not the assigned one cannot join or act.
    with (
        runtime_role(),
        tenant_context(other_physician.pk, graph.organization_a),
        pytest.raises(TeleconsultAccessDeniedError),
    ):
        request_physician_join(clinic_id=graph.clinic_a, session_id=session.pk)
    with (
        runtime_role(),
        tenant_context(other_physician.pk, graph.organization_a),
        pytest.raises(TeleconsultAccessDeniedError),
    ):
        start_consultation(clinic_id=graph.clinic_a, session_id=session.pk)
    # The receptionist cannot create or join sessions.
    with (
        runtime_role(),
        tenant_context(graph.shared_user, graph.organization_a),
        pytest.raises(TeleconsultAccessDeniedError),
    ):
        create_session(clinic_id=graph.clinic_a, encounter_id=uuid4())
    with (
        runtime_role(),
        tenant_context(graph.shared_user, graph.organization_a),
        pytest.raises(TeleconsultAccessDeniedError),
    ):
        request_physician_join(clinic_id=graph.clinic_a, session_id=session.pk)
    # Cross-tenant scope never enumerates the session.
    with (
        runtime_role(),
        tenant_context(graph.shared_user, graph.organization_b),
        pytest.raises(TeleconsultAccessDeniedError),
    ):
        start_consultation(clinic_id=graph.clinic_a, session_id=session.pk)
    physician_token = _physician_join(graph, session)
    patient_token = _patient_join(patient_session, session)
    # Role and participant swaps: each token only enters its own binding.
    with (
        runtime_role(),
        patient_session_context(patient_session),
        pytest.raises(TeleconsultAccessDeniedError),
    ):
        enter_room(token=physician_token, role="patient")
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(TeleconsultAccessDeniedError),
    ):
        enter_room(token=patient_token, role="physician")
    with (
        runtime_role(),
        tenant_context(other_physician.pk, graph.organization_a),
        pytest.raises(TeleconsultAccessDeniedError),
    ):
        enter_room(token=physician_token, role="physician")
    # A different patient cannot use the bound patient's token.
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        other = create_patient(
            clinic_id=graph.clinic_a,
            full_name="Outro Paciente Sintético",
            birth_date=date(1992, 1, 1),
            idempotency_key=uuid4(),
        )
        invitation = issue_invitation(
            clinic_id=graph.clinic_a, enrollment_id=other.enrollment.pk
        )
    with runtime_role():
        other_session = redeem_invitation(graph.clinic_a, invitation.secret)
    assert other_session is not None
    with (
        runtime_role(),
        patient_session_context(other_session),
        pytest.raises(PatientAccessDeniedError),
    ):
        request_patient_join(session_id=session.pk)
    with (
        runtime_role(),
        patient_session_context(other_session),
        pytest.raises(TeleconsultAccessDeniedError),
    ):
        enter_room(token=patient_token, role="patient")
    # The intended participants still enter after every denial above.
    assert _physician_enter(graph, physician_token).room_name == (f"tc-{session.pk}")
    assert _patient_enter(patient_session, patient_token).room_name == (
        f"tc-{session.pk}"
    )


def test_expired_and_rotated_credentials_cannot_enter(
    rbac_graph: RbacGraph,
    synthetic_provider: SyntheticRoomAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = rbac_graph
    _, encounter, _, _, _ = seed(graph)
    session = _create(graph, encounter)
    assert _run_room(graph, session) == "succeeded"
    expired_token = _physician_join(graph, session)
    # Deterministic expiry: the credential outlives its 15-minute window.
    real_now = timezone.now
    monkeypatch.setattr(
        timezone,
        "now",
        lambda: real_now() + timedelta(minutes=20),
    )
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(TeleconsultAccessDeniedError),
    ):
        enter_room(token=expired_token, role="physician")
    monkeypatch.setattr(timezone, "now", real_now)
    live_token = _physician_join(graph, session)
    rotated_token = _physician_join(graph, session)
    assert live_token != rotated_token
    # Rotation revoked the earlier credential; only the newest enters.
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(TeleconsultAccessDeniedError),
    ):
        enter_room(token=live_token, role="physician")
    assert _physician_enter(graph, rotated_token).room_name == (f"tc-{session.pk}")
    kinds = _kinds(graph, session)
    assert kinds[0] == "created"
    assert kinds.count("join_denied") == 2
    assert kinds.count("joined") == 1
    assert len(kinds) == 4


def test_raw_mutation_reopen_and_capture_flag_fail_closed(
    rbac_graph: RbacGraph, synthetic_provider: SyntheticRoomAdapter
) -> None:
    graph = rbac_graph
    _, encounter, _, _, _ = seed(graph)
    session = _create(graph, encounter)
    assert _run_room(graph, session) == "succeeded"
    token = _physician_join(graph, session)
    _physician_enter(graph, token)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        end_consultation(clinic_id=graph.clinic_a, session_id=session.pk)
    tables = [
        "teleconsult_teleconsultsession",
        "teleconsult_teleconsultroom",
        "teleconsult_teleconsultcredential",
        "teleconsult_teleconsultevent",
    ]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class "
            "WHERE relnamespace='clinic_app'::regnamespace AND relname=ANY(%s)",
            [tables],
        )
        assert set(cursor.fetchall()) == {(table, True, True) for table in tables}
        for table in tables:
            cursor.execute(
                "SELECT has_table_privilege('clinic_app', %s, 'DELETE')",
                [f"clinic_app.{table}"],
            )
            assert cursor.fetchone() == (False,)
    with setup_context(graph.organization_a):
        room = TeleconsultRoom.objects.get(session_id=session.pk)
        event = TeleconsultEvent.objects.filter(session_id=session.pk).first()
        credential = TeleconsultCredential.objects.filter(session_id=session.pk).first()
        assert event is not None
        assert credential is not None
        # Terminal sessions never reopen; rooms and events are immutable.
        with pytest.raises(DatabaseError), transaction.atomic():
            TeleconsultSession.objects.filter(pk=session.pk).update(
                state="waiting", ended_at=None, revision=2
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            TeleconsultRoom.objects.filter(pk=room.pk).update(room_name="other")
        with pytest.raises(DatabaseError), transaction.atomic():
            TeleconsultEvent.objects.filter(pk=event.pk).update(kind="joined")
        with pytest.raises(DatabaseError), transaction.atomic():
            TeleconsultCredential.objects.filter(pk=credential.pk).update(
                token_digest="0" * 64
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            TeleconsultSession.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                encounter_id=encounter.pk,
                appointment_id=session.appointment_id,
                patient_id=uuid4(),
                physician_id=session.physician_id,
                consent_id=session.consent_id,
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            TeleconsultRoom.objects.create(
                organization_id=graph.organization_a,
                operation_id=uuid4(),
                session_id=session.pk,
                room_name="forged",
                recording_enabled=True,
            )
        for model, pk in (
            (TeleconsultSession, session.pk),
            (TeleconsultRoom, room.pk),
            (TeleconsultEvent, event.pk),
            (TeleconsultCredential, credential.pk),
        ):
            with (
                pytest.raises(DatabaseError),
                transaction.atomic(),
                connection.cursor() as cursor,
            ):
                cursor.execute(
                    sql.SQL("DELETE FROM clinic_app.{} WHERE id=%s").format(
                        sql.Identifier(model._meta.db_table)
                    ),
                    [str(pk)],
                )
    # The stored objects survive every rejected mutation.
    with setup_context(graph.organization_a):
        session.refresh_from_db()
        assert session.state == "ended"
        assert TeleconsultRoom.objects.get(pk=room.pk).room_name == (f"tc-{session.pk}")
        assert TeleconsultEvent.objects.filter(session_id=session.pk).exists()


def test_room_and_session_bindings_are_not_swappable(
    rbac_graph: RbacGraph, synthetic_provider: SyntheticRoomAdapter
) -> None:
    graph = rbac_graph
    appointment_a, encounter_a, _, patient_session, _ = seed(graph)
    session_a = _create(graph, encounter_a)
    # A second appointment/encounter for the same physician and patient.
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        enrollment = PatientClinicEnrollment.objects.get(
            clinic_id=graph.clinic_a, patient_id=appointment_a.patient_id
        )
        appointment_b = create_appointment(
            clinic_id=graph.clinic_a,
            enrollment_id=enrollment.pk,
            practitioner_id=graph.physician,
            local_range=AppointmentLocalRange("2035-06-02T10:30", "2035-06-02T11:00"),
            idempotency_key=uuid4(),
        )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        encounter_b = open_encounter(
            clinic_id=graph.clinic_a, appointment_id=appointment_b.pk
        )
    session_b = _create(graph, encounter_b)
    assert session_b.pk != session_a.pk
    assert _run_room(graph, session_a) == "succeeded"
    assert _run_room(graph, session_b) == "succeeded"
    # Credentials never cross sessions: each token resolves only its own room.
    token_a = _physician_join(graph, session_a)
    token_b = _physician_join(graph, session_b)
    entry_a = _physician_enter(graph, token_a)
    entry_b = _physician_enter(graph, token_b)
    assert entry_a.session.pk == session_a.pk
    assert entry_b.session.pk == session_b.pk
    assert entry_a.room_name != entry_b.room_name
    patient_token_b = _patient_join(patient_session, session_b)
    assert _patient_enter(patient_session, patient_token_b).room_name == (
        entry_b.room_name
    )
    # A stored room row cannot be rebound to another session or operation.
    with setup_context(graph.organization_a):
        room_a = TeleconsultRoom.objects.get(session_id=session_a.pk)
        with pytest.raises(DatabaseError), transaction.atomic():
            TeleconsultRoom.objects.create(
                organization_id=graph.organization_a,
                operation_id=room_a.operation_id,
                session_id=session_b.pk,
                room_name=f"tc-{session_b.pk}",
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            TeleconsultSession.objects.filter(pk=session_a.pk).update(
                encounter_id=encounter_b.pk, revision=2
            )
        assert derived_state(session_a) == "waiting"
        assert derived_state(session_b) == "waiting"


def test_fail_transition_enforces_stored_authority_and_reason(
    rbac_graph: RbacGraph,
    synthetic_provider: SyntheticRoomAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """teleconsult_fail cannot be driven by foreign, wrong-role or false input."""
    graph = rbac_graph
    appointment, encounter, acceptance, patient_session, manager_id = seed(graph)
    session = _create(graph, encounter)
    assert _run_room(graph, session) == "succeeded"
    other_physician = create_role_actor(graph, UserClinicRole.Role.PHYSICIAN)
    # A consent-only patient session for the same patient lacks the
    # teleconsult operation, so it must not carry fail authority either.
    with runtime_role(), tenant_context(manager_id, graph.organization_a):
        enrollment = PatientClinicEnrollment.objects.get(
            clinic_id=graph.clinic_a, patient_id=appointment.patient_id
        )
        monkeypatch.setattr(
            "apps.intake.patient_access.PATIENT_OPERATION_VALUES", ["consent"]
        )
        invitation = issue_invitation(
            clinic_id=graph.clinic_a, enrollment_id=enrollment.pk
        )
    with runtime_role():
        consent_only_session = redeem_invitation(graph.clinic_a, invitation.secret)
    assert consent_only_session is not None
    # Foreign-tenant staff and a consent-only patient session cannot even
    # resolve the stored scope, let alone terminate the session.
    denied_contexts = (
        tenant_context(graph.shared_user, graph.organization_b),
        patient_session_context(consent_only_session),
    )
    for context in denied_contexts:
        with runtime_role(), context, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM clinic_app.teleconsult_session_scope(%s)",
                [str(session.pk)],
            )
            assert cursor.fetchone() is None
            cursor.execute(
                "SELECT clinic_app.teleconsult_fail(%s,%s)",
                [str(session.pk), "consent_revoked"],
            )
            assert cursor.fetchone() == (False,)
    # A same-tenant physician who is not the assigned one can read the
    # scope (the RLS read policy already allows clinic staff) but still
    # cannot terminate the session.
    with (
        runtime_role(),
        tenant_context(other_physician.pk, graph.organization_a),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT clinic_app.teleconsult_fail(%s,%s)",
            [str(session.pk), "consent_revoked"],
        )
        assert cursor.fetchone() == (False,)
    # The bound patient cannot fail the session for a reason the stored
    # rows do not show: consent is still the current unrevoked version.
    with runtime_role(), patient_session_context(patient_session):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.teleconsult_fail(%s,%s)",
                [str(session.pk), "consent_revoked"],
            )
            assert cursor.fetchone() == (False,)
            cursor.execute(
                "SELECT clinic_app.teleconsult_fail(%s,%s)",
                [str(session.pk), "room_unavailable"],
            )
            assert cursor.fetchone() == (False,)
        # Once the stored consent is revoked, the same authority persists
        # the real terminal failure.
        revoke_consent(acceptance_id=acceptance.pk)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.teleconsult_fail(%s,%s)",
                [str(session.pk), "consent_revoked"],
            )
            assert cursor.fetchone() == (True,)
    with setup_context(graph.organization_a):
        session.refresh_from_db()
        assert session.state == "failed"
        assert session.failure_reason == "consent_revoked"
    assert _kinds(graph, session) == ["created", "failed"]


def test_revoked_credential_cannot_be_unrevoked(
    rbac_graph: RbacGraph, synthetic_provider: SyntheticRoomAdapter
) -> None:
    """revoked_at and first_used_at are write-once at the database boundary."""
    graph = rbac_graph
    _, encounter, _, patient_session, _ = seed(graph)
    session = _create(graph, encounter)
    assert _run_room(graph, session) == "succeeded"
    old_token = _patient_join(patient_session, session)
    _patient_enter(patient_session, old_token)
    _patient_join(patient_session, session)  # rotation revokes old_token
    old_digest = hashlib.sha256(old_token.encode()).hexdigest()
    with runtime_role(), patient_session_context(patient_session):
        with pytest.raises(TeleconsultAccessDeniedError):
            enter_room(token=old_token, role="patient")
        credential = TeleconsultCredential.objects.get(
            session_id=session.pk, token_digest=old_digest
        )
        assert credential.revoked_at is not None
        assert credential.first_used_at is not None
        with pytest.raises(DatabaseError), transaction.atomic():
            TeleconsultCredential.objects.filter(pk=credential.pk).update(
                revoked_at=None
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            TeleconsultCredential.objects.filter(pk=credential.pk).update(
                first_used_at=None
            )
        with pytest.raises(TeleconsultAccessDeniedError):
            enter_room(token=old_token, role="patient")
    with setup_context(graph.organization_a):
        credential.refresh_from_db()
        assert credential.revoked_at is not None
        assert credential.first_used_at is not None
