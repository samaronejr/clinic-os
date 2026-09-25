"""Purpose taxonomy, notices, refusals, participant and AI-use records."""

from __future__ import annotations

from datetime import date
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.consent.models import (
    AIUseDisclosure,
    ConsentPurpose,
    ConsentText,
    NoticeTopic,
    NoticeVersion,
    ParticipantAcknowledgment,
    ParticipantKind,
    RefusalRecord,
)
from apps.consent.services import (
    acknowledge_participant,
    ai_disclosure_status,
    available_notices,
    available_texts,
    consent_for_future_use,
    patient_refusals,
    prepare_acceptance,
    publish_notice,
    publish_text,
    record_ai_disclosure,
    record_consent,
    record_refusal,
    staff_refusals,
)
from apps.ehr.models import Encounter
from apps.ehr.services import SOAP_FIELDS, open_encounter, record_clinical_note
from apps.identity.current_context import CurrentActorError
from apps.identity.models import User, UserClinicRole
from apps.intake.access import PatientAccessDeniedError
from apps.intake.models import PatientClinicEnrollment
from apps.intake.patient_access import (
    issue_invitation,
    patient_session_context,
    redeem_invitation,
)
from apps.intake.services import create_patient
from apps.tenancy.db import tenant_context
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.utils import timezone, translation
from django.utils.translation import gettext

from auth.stepup_test_support import create_role_actor
from patient_service_support import runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL
from renewal.test_consent import accept as accept_offered
from renewal.test_consent import patient_client
from renewal.test_consent import seed as consent_seed
from renewal.test_encounters import (
    draft,
    seed_appointment_setup_for_existing,
    setup_context,
)
from renewal.test_encounters import seed as clinical_seed
from renewal.test_retention import admin, staff_client
from scheduling.appointment_service_support import create_synthetic_appointment

if TYPE_CHECKING:
    from uuid import UUID

    from apps.scheduling.models import Appointment

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

RECORDING_TEXT = (
    "Texto sintético de gravação da consulta.\n\n"
    "Escolha livre e específica; não autoriza outros usos."
)
AI_TEXT = (
    "Texto sintético de assistência por IA.\n\n"
    "Escolha livre e específica; recusar não impede o atendimento."
)
NOTICE_TEXT = (
    "Aviso sintético: a clínica pode gravar consultas e usar IA de apoio.\n\n"
    "Este aviso informa e não substitui consentimento."
)
NEW_PURPOSES = [
    "consultation_recording",
    "ai_assistance",
    "transactional_messaging",
    "marketing",
    "research_model_improvement",
]


def _patient_session(graph: RbacGraph) -> tuple[UUID, UUID]:
    """Create one synthetic patient and redeem a consent-capable session."""
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        registration = create_patient(
            clinic_id=graph.clinic_a,
            full_name="Paciente Sintético Taxonomia",
            birth_date=date(1991, 2, 2),
            idempotency_key=uuid4(),
        )
        invitation = issue_invitation(
            clinic_id=graph.clinic_a, enrollment_id=registration.enrollment.pk
        )
    with runtime_role():
        session = redeem_invitation(graph.clinic_a, invitation.secret)
    assert session is not None
    return session, registration.enrollment.pk


def test_full_purpose_taxonomy_publish_accept_and_future_use(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    manager = admin(graph)
    session, enrollment = _patient_session(graph)
    texts: dict[str, ConsentText] = {}
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        for index, purpose in enumerate(NEW_PURPOSES):
            texts[purpose] = publish_text(
                clinic_id=graph.clinic_a,
                purpose=purpose,
                text=f"{RECORDING_TEXT}\nFinalidade sintética {index}.",
            )
    with runtime_role(), patient_session_context(session):
        available = {text.purpose: text for text in available_texts()}
        assert set(available) == set(NEW_PURPOSES)
        for purpose in NEW_PURPOSES:
            _, offer = prepare_acceptance(text_id=texts[purpose].pk)
            receipt = record_consent(offer=offer, purpose=purpose, accepted=True)
            assert receipt.authority == "patient_explicit_action"
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        for purpose in NEW_PURPOSES:
            active = consent_for_future_use(
                clinic_id=graph.clinic_a, enrollment_id=enrollment, purpose=purpose
            )
            assert active is not None
            assert active.text.purpose == purpose
        # Purposes never published still fail closed.
        assert (
            consent_for_future_use(
                clinic_id=graph.clinic_a,
                enrollment_id=enrollment,
                purpose="teleconsultation",
            )
            is None
        )
        # A purpose outside the taxonomy is rejected, never silently None.
        with pytest.raises(ValidationError):
            consent_for_future_use(
                clinic_id=graph.clinic_a,
                enrollment_id=enrollment,
                purpose="location_tracking",
            )


def test_forged_purpose_rejected_by_service_and_database_check(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    manager = admin(graph)
    with (
        runtime_role(),
        tenant_context(manager.pk, graph.organization_a),
        pytest.raises(ValidationError),
    ):
        publish_text(
            clinic_id=graph.clinic_a, purpose="blanket_care", text=RECORDING_TEXT
        )
    with (
        runtime_role(),
        tenant_context(manager.pk, graph.organization_a),
        pytest.raises(ValidationError),
    ):
        publish_notice(
            clinic_id=graph.clinic_a,
            topic="marketing",
            text=NOTICE_TEXT,
        )
    with (
        runtime_role(),
        tenant_context(manager.pk, graph.organization_a),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        ConsentText.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            purpose="blanket_care",
            version=1,
            text=RECORDING_TEXT,
            digest=sha256(RECORDING_TEXT.encode()).hexdigest(),
            published_by_id=manager.pk,
        )
    with (
        runtime_role(),
        tenant_context(manager.pk, graph.organization_a),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        ConsentText.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            purpose="marketing",
            version=1,
            text=RECORDING_TEXT,
            digest=sha256(RECORDING_TEXT.encode()).hexdigest(),
            published_by_id=uuid4(),
        )


def test_refusal_is_recorded_idempotent_and_never_cancels_care(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    manager = admin(graph)
    session, enrollment = _patient_session(graph)
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        text = publish_text(
            clinic_id=graph.clinic_a, purpose="ai_assistance", text=AI_TEXT
        )
    with runtime_role(), patient_session_context(session):
        _, offer = prepare_acceptance(text_id=text.pk)
        refusal = record_refusal(offer=offer, purpose="ai_assistance")
        # Retrying the same refusal converges on the same record.
        assert record_refusal(offer=offer, purpose="ai_assistance").pk == refusal.pk
        assert refusal.authority == "patient_explicit_action"
        assert patient_refusals()[0].pk == refusal.pk
        # A refusal is not an acceptance: future use fails closed.
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        assert (
            consent_for_future_use(
                clinic_id=graph.clinic_a,
                enrollment_id=enrollment,
                purpose="ai_assistance",
            )
            is None
        )
        assert (
            staff_refusals(clinic_id=graph.clinic_a, enrollment_id=enrollment)[0].pk
            == refusal.pk
        )
    with setup_context(graph.organization_a):
        refused_events = list(AuditEvent.objects.filter(event_type="consent.refused"))
        assert len(refused_events) == 1
        assert refused_events[0].actor_user_id == session
        assert set(refused_events[0].payload) == {"clinic_id", "object_verb"}
    # Changing one's mind is allowed: a fresh explicit action accepts the
    # exact same version after refusal.
    with runtime_role(), patient_session_context(session):
        _, offer = prepare_acceptance(text_id=text.pk)
        receipt = record_consent(offer=offer, purpose="ai_assistance", accepted=True)
        assert receipt.text_id == text.pk
        # Refusing an active acceptance is rejected; the patient revokes.
        with pytest.raises(ValidationError):
            record_refusal(offer=offer, purpose="ai_assistance")


def test_refusal_requires_current_version_and_own_session(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    manager = admin(graph)
    session, _ = _patient_session(graph)
    other_session, _ = _patient_session(graph)
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        text = publish_text(
            clinic_id=graph.clinic_a,
            purpose="consultation_recording",
            text=RECORDING_TEXT,
        )
    with runtime_role(), patient_session_context(session):
        _, offer = prepare_acceptance(text_id=text.pk)
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        publish_text(
            clinic_id=graph.clinic_a,
            purpose="consultation_recording",
            text=RECORDING_TEXT + "\nNova versão.",
        )
    # A refusal of a superseded version is a conflict, not a record.
    with (
        runtime_role(),
        patient_session_context(session),
        pytest.raises(ValidationError),
    ):
        record_refusal(offer=offer, purpose="consultation_recording")
    # Another session cannot replay the offer, even to refuse.
    with runtime_role(), patient_session_context(other_session):
        with pytest.raises(PatientAccessDeniedError):
            record_refusal(offer=offer, purpose="consultation_recording")
        assert patient_refusals() == []


def test_notices_inform_without_authorizing(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    manager = admin(graph)
    session, enrollment = _patient_session(graph)
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        first = publish_notice(
            clinic_id=graph.clinic_a, topic="ai_use", text=NOTICE_TEXT
        )
        second = publish_notice(
            clinic_id=graph.clinic_a,
            topic="ai_use",
            text=NOTICE_TEXT + "\nAtualização.",
        )
        assert (first.version, second.version) == (1, 2)
    with runtime_role(), patient_session_context(session):
        notices = available_notices()
        assert len(notices) == 1
        assert notices[0].pk == second.pk
        # A notice is never an acceptance source.
        with pytest.raises(PatientAccessDeniedError):
            prepare_acceptance(text_id=second.pk)
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        assert (
            consent_for_future_use(
                clinic_id=graph.clinic_a,
                enrollment_id=enrollment,
                purpose="ai_assistance",
            )
            is None
        )
    with setup_context(graph.organization_a):
        published = AuditEvent.objects.filter(event_type="consent.notice.published")
        assert published.count() == 2


def _encounter(graph: RbacGraph, appointment: Appointment) -> UUID:
    """Open one scheduled appointment as an encounter under the physician."""
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        return open_encounter(
            clinic_id=graph.clinic_a, appointment_id=appointment.pk
        ).pk


def test_ai_use_disclosure_once_per_encounter_and_refusal_preserved(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    appointment, _ = clinical_seed(graph)
    encounter_id = _encounter(graph, appointment)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        disclosure = record_ai_disclosure(
            clinic_id=graph.clinic_a,
            encounter_id=encounter_id,
            informed=True,
            refused=True,
        )
        assert disclosure.recorded_by_id == graph.physician
        same = record_ai_disclosure(
            clinic_id=graph.clinic_a,
            encounter_id=encounter_id,
            informed=True,
            refused=True,
        )
        assert same.pk == disclosure.pk
        # A conflicting second attestation is rejected; the row stays intact.
        with pytest.raises(ValidationError):
            record_ai_disclosure(
                clinic_id=graph.clinic_a,
                encounter_id=encounter_id,
                informed=True,
                refused=False,
            )
        status = ai_disclosure_status(
            clinic_id=graph.clinic_a, encounter_id=encounter_id
        )
        assert status is not None
        assert status.refused
    # Org-B membership can never authorize a clinic-A disclosure.
    with (
        runtime_role(),
        tenant_context(graph.shared_user, graph.organization_b),
        pytest.raises(CurrentActorError),
    ):
        record_ai_disclosure(
            clinic_id=graph.clinic_a,
            encounter_id=encounter_id,
            informed=True,
            refused=False,
        )
    with (
        runtime_role(),
        tenant_context(graph.shared_user, graph.organization_a),
        pytest.raises(CurrentActorError),
    ):
        record_ai_disclosure(
            clinic_id=graph.clinic_a,
            encounter_id=encounter_id,
            informed=True,
            refused=False,
        )
    # Refusal implies informed: the DB check rejects refused-without-informed.
    setup = seed_appointment_setup_for_existing(graph, appointment)
    with (
        runtime_role(),
        tenant_context(graph.shared_user, graph.organization_a),
    ):
        second = create_synthetic_appointment(
            setup, start_local="2035-06-02T10:00", end_local="2035-06-02T11:00"
        )
    other = _encounter(graph, second)
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(ValidationError),
    ):
        record_ai_disclosure(
            clinic_id=graph.clinic_a,
            encounter_id=other,
            informed=False,
            refused=True,
        )


def test_participant_acknowledgment_for_non_patient_voices(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    appointment, _ = clinical_seed(graph)
    encounter_id = _encounter(graph, appointment)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        caregiver = acknowledge_participant(
            clinic_id=graph.clinic_a,
            session_id=encounter_id,
            participant_kind="caregiver",
        )
        assert caregiver.acknowledged_by_clinician_id == graph.physician
        again = acknowledge_participant(
            clinic_id=graph.clinic_a,
            session_id=encounter_id,
            participant_kind="caregiver",
        )
        assert again.pk == caregiver.pk
        interpreter = acknowledge_participant(
            clinic_id=graph.clinic_a,
            session_id=encounter_id,
            participant_kind="interpreter",
        )
        assert interpreter.pk != caregiver.pk
        # Retries converge on the same row without duplicating the audit event.
        again_interpreter = acknowledge_participant(
            clinic_id=graph.clinic_a,
            session_id=encounter_id,
            participant_kind="interpreter",
        )
        assert again_interpreter.pk == interpreter.pk
        with pytest.raises(ValidationError):
            acknowledge_participant(
                clinic_id=graph.clinic_a,
                session_id=encounter_id,
                participant_kind="driver",
            )
        with pytest.raises(PatientAccessDeniedError):
            acknowledge_participant(
                clinic_id=graph.clinic_a,
                session_id=uuid4(),
                participant_kind="companion",
            )
    with (
        runtime_role(),
        tenant_context(graph.shared_user, graph.organization_a),
        pytest.raises(CurrentActorError),
    ):
        acknowledge_participant(
            clinic_id=graph.clinic_a,
            session_id=encounter_id,
            participant_kind="companion",
        )
    # No patient record is created for acknowledged participants.
    with setup_context(graph.organization_a):
        assert ParticipantAcknowledgment.objects.count() == 2
        events = AuditEvent.objects.filter(
            event_type="consent.participant.acknowledged"
        )
        assert [event.affected_record_id for event in events] == [
            str(caregiver.pk),
            str(interpreter.pk),
        ]
        for event in events:
            assert set(event.payload) <= {
                "clinic_id",
                "http_method",
                "http_status",
                "object_verb",
                "reason_code",
                "request_id",
            }


def test_taxonomy_tables_hold_no_destructive_privileges_and_stay_isolated(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    text, session, _, _ = consent_seed(graph)
    appointment, _ = clinical_seed(graph)
    encounter_id = _encounter(graph, appointment)
    with runtime_role(), patient_session_context(session):
        accept_offered(text)
    refusal = _refuse(graph, session, "ai_assistance")
    manager = admin(graph)
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        publish_notice(
            clinic_id=graph.clinic_a, topic="care_processing", text=NOTICE_TEXT
        )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        record_ai_disclosure(
            clinic_id=graph.clinic_a,
            encounter_id=encounter_id,
            informed=True,
            refused=False,
        )
        acknowledge_participant(
            clinic_id=graph.clinic_a,
            session_id=encounter_id,
            participant_kind="companion",
        )
    assert refusal is not None
    tables = [
        "consent_noticeversion",
        "consent_refusalrecord",
        "consent_participantacknowledgment",
        "consent_aiusedisclosure",
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
                "SELECT has_table_privilege('clinic_app', %s, 'DELETE'), "
                "has_table_privilege('clinic_app', %s, 'UPDATE')",
                [f"clinic_app.{table}"] * 2,
            )
            assert cursor.fetchone() == (False, False)
    # Populated foreign-organization probe: every taxonomy table stays empty.
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_b):
        assert NoticeVersion.objects.count() == 0
        assert RefusalRecord.objects.count() == 0
        assert ParticipantAcknowledgment.objects.count() == 0
        assert AIUseDisclosure.objects.count() == 0
    # Immutable history: even owner-context writes cannot rewrite records.
    with setup_context(graph.organization_a):
        notice = NoticeVersion.objects.get()
        with pytest.raises(DatabaseError), transaction.atomic():
            NoticeVersion.objects.filter(pk=notice.pk).update(topic="ai_use")
        with pytest.raises(DatabaseError), transaction.atomic():
            NoticeVersion.objects.filter(pk=notice.pk).delete()
        assert ParticipantAcknowledgment.objects.count() == 1
        with pytest.raises(DatabaseError), transaction.atomic():
            ParticipantAcknowledgment.objects.update(participant_kind="interpreter")
        with pytest.raises(DatabaseError), transaction.atomic():
            ParticipantAcknowledgment.objects.all().delete()
        assert AIUseDisclosure.objects.count() == 1
        with pytest.raises(DatabaseError), transaction.atomic():
            AIUseDisclosure.objects.update(informed=False)
        with pytest.raises(DatabaseError), transaction.atomic():
            AIUseDisclosure.objects.all().delete()


def _refuse(graph: RbacGraph, session: UUID, purpose: str) -> RefusalRecord:
    """Publish if needed and record one refusal inside the patient session."""
    manager = admin(graph)
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        text = (
            ConsentText.objects.filter(clinic_id=graph.clinic_a, purpose=purpose)
            .order_by("-version")
            .first()
        )
        if text is None:
            text = publish_text(
                clinic_id=graph.clinic_a,
                purpose=purpose,
                text=f"{RECORDING_TEXT}\nFinalidade {purpose}.",
            )
    with runtime_role(), patient_session_context(session):
        _, offer = prepare_acceptance(text_id=text.pk)
        return record_refusal(offer=offer, purpose=purpose)


def _foreign_org_patient_session(graph: RbacGraph) -> UUID:
    """Redeem a consent session for a synthetic patient of organization B."""
    with setup_context(graph.organization_b):
        manager = User.objects.create(
            username=f"todo20-orgb-receptionist-{uuid4().hex}",
            password=make_password(RBAC_RAW_CREDENTIAL),
        )
        UserClinicRole.objects.create(
            organization_id=graph.organization_b,
            clinic_id=graph.clinic_c,
            user_id=manager.pk,
            role=UserClinicRole.Role.RECEPTIONIST,
        )
    with runtime_role(), tenant_context(manager.pk, graph.organization_b):
        registration = create_patient(
            clinic_id=graph.clinic_c,
            full_name="Paciente Sintético Outra Organização",
            birth_date=date(1992, 3, 3),
            idempotency_key=uuid4(),
        )
        invitation = issue_invitation(
            clinic_id=graph.clinic_c, enrollment_id=registration.enrollment.pk
        )
    with runtime_role():
        session = redeem_invitation(graph.clinic_c, invitation.secret)
    assert session is not None
    return session


def test_ai_disclosure_read_is_bound_to_the_disclosed_patient(
    rbac_graph: RbacGraph,
) -> None:
    """A patient session reads only its own disclosures, never foreign ones."""
    graph = rbac_graph
    appointment, _ = clinical_seed(graph)
    encounter_id = _encounter(graph, appointment)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        disclosure = record_ai_disclosure(
            clinic_id=graph.clinic_a,
            encounter_id=encounter_id,
            informed=True,
            refused=True,
        )
    # An unrelated patient session in the same clinic sees no disclosure.
    other_session, _ = _patient_session(graph)
    with runtime_role(), patient_session_context(other_session):
        assert not AIUseDisclosure.objects.filter(pk=disclosure.pk).exists()
        assert AIUseDisclosure.objects.count() == 0
    # A patient session in another organization sees nothing either.
    foreign_session = _foreign_org_patient_session(graph)
    with runtime_role(), patient_session_context(foreign_session):
        assert not AIUseDisclosure.objects.filter(pk=disclosure.pk).exists()
        assert AIUseDisclosure.objects.count() == 0
    # The disclosed patient's own session reads exactly its own row.
    with setup_context(graph.organization_a):
        enrollment = PatientClinicEnrollment.objects.get(
            patient_id=Encounter.objects.get(pk=encounter_id).patient_id,
            clinic_id=graph.clinic_a,
        )
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        invitation = issue_invitation(
            clinic_id=graph.clinic_a, enrollment_id=enrollment.pk
        )
    with runtime_role():
        own_session = redeem_invitation(graph.clinic_a, invitation.secret)
    assert own_session is not None
    with runtime_role(), patient_session_context(own_session):
        assert list(AIUseDisclosure.objects.all()) == [disclosure]


def test_purpose_labels_render_in_portuguese() -> None:
    """Every taxonomy label resolves through gettext under the pt-BR catalog."""
    with translation.override("pt-br"):
        for purpose in ConsentPurpose:
            assert str(purpose.label) == gettext(str(purpose.label))
        for topic in NoticeTopic:
            assert str(topic.label) == gettext(str(topic.label))
        for kind in ParticipantKind:
            assert str(kind.label) == gettext(str(kind.label))
        # The catalog actually translates: never an English echo.
        assert gettext("Consultation recording") != "Consultation recording"


def test_refusal_records_are_immutable_under_owner_and_runtime_roles(
    rbac_graph: RbacGraph,
) -> None:
    """Populated refusal rows reject UPDATE and DELETE for every principal."""
    graph = rbac_graph
    session, _ = _patient_session(graph)
    refusal = _refuse(graph, session, "ai_assistance")
    with setup_context(graph.organization_a):
        assert RefusalRecord.objects.count() == 1
        with pytest.raises(DatabaseError), transaction.atomic():
            RefusalRecord.objects.filter(pk=refusal.pk).update(
                refused_at=timezone.now()
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            RefusalRecord.objects.filter(pk=refusal.pk).delete()
        assert RefusalRecord.objects.get(pk=refusal.pk) is not None
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        with pytest.raises(DatabaseError), transaction.atomic():
            RefusalRecord.objects.filter(pk=refusal.pk).update(
                refused_at=timezone.now()
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            RefusalRecord.objects.filter(pk=refusal.pk).delete()


def test_refusal_never_blocks_manual_clinical_note(rbac_graph: RbacGraph) -> None:
    """A recorded AI-assistance refusal must not gate the manual SOAP path."""
    graph = rbac_graph
    appointment, template = clinical_seed(graph)
    _encounter(graph, appointment)
    session, _ = _patient_session(graph)
    _refuse(graph, session, "ai_assistance")
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = draft(graph, appointment, template)
        saved = record_clinical_note(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            expected_revision=1,
            content=dict.fromkeys(SOAP_FIELDS, "Sintético após recusa"),
        )
        assert saved.revision == 2


@pytest.mark.parametrize("action", ["disclosure", "acknowledge"])
def test_staff_clinical_actions_require_physician_and_deny_identically(
    rbac_graph: RbacGraph, action: str
) -> None:
    """Every non-physician role, wrong clinic and unknown session share 403."""
    graph = rbac_graph
    appointment, _ = clinical_seed(graph)
    encounter_id = _encounter(graph, appointment)
    url = f"/clinics/{graph.clinic_a}/consent/"
    unknown = str(uuid4())

    def payload(target: str) -> dict[str, str]:
        if action == "disclosure":
            return {
                "action": action,
                "disclosure-encounter_id": target,
                "disclosure-informed": "on",
            }
        return {
            "action": action,
            "ack-session_id": target,
            "ack-participant_kind": "caregiver",
        }

    # The physician succeeds on the real encounter and denies on an unknown id.
    with staff_client(graph.physician) as staff:
        reference = staff.post(url, payload(unknown))
        assert reference.status_code == 403
        assert staff.post(url, payload(str(encounter_id))).status_code == 200

    # A physician on a different clinic and every other role deny identically;
    # the denial is independent of whether the encounter exists.
    def deny_identically(actor_pk: UUID) -> None:
        with staff_client(actor_pk) as staff:
            denied = staff.post(url, payload(str(encounter_id)))
            assert denied.status_code == 403
            denied_unknown = staff.post(url, payload(unknown))
            assert denied_unknown.status_code == 403
            # Same actor, same surface: existence of the record must not leak
            # through a different denial body or status.
            assert denied.content == denied_unknown.content

    # A physician on a different clinic and every other role deny identically.
    for role in (
        UserClinicRole.Role.OWNER,
        UserClinicRole.Role.RECEPTIONIST,
        UserClinicRole.Role.CLINIC_ADMIN,
        UserClinicRole.Role.NURSE,
        UserClinicRole.Role.ALLIED_PROFESSIONAL,
        UserClinicRole.Role.FINANCE,
    ):
        deny_identically(create_role_actor(graph, role).pk)
    # Mixed roles cannot widen the physician scope.
    mixed = create_role_actor(graph, UserClinicRole.Role.NURSE)
    with setup_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=mixed.pk,
            role=UserClinicRole.Role.RECEPTIONIST,
        )
    deny_identically(mixed.pk)
    # An org-B physician holds no clinic-A role; the denial stays identical.
    deny_identically(graph.shared_user)
    # A patient session never reaches the staff surface.
    session, _ = _patient_session(graph)
    patient = patient_client(session)
    assert patient.post(url, payload(str(encounter_id))).status_code == 403
