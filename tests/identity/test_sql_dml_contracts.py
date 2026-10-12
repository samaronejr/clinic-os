"""Real trigger members mint exact stored-row verdicts, never Boolean wrappers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, tzinfo
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.consent.models import (
    AIUseDisclosure,
    ConsentAcceptance,
    ConsentRevocation,
    ConsentText,
    NoticeVersion,
    ParticipantAcknowledgment,
    RefusalRecord,
)
from apps.ehr.services import open_encounter
from apps.identity.models import User, UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from apps.intake.models import PatientClinicEnrollment, PatientSession
from apps.intake.patient_access import (
    issue_invitation,
    patient_session_context,
    redeem_invitation,
)
from apps.intake.services import create_patient
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from auth.stepup_test_support import create_role_actor
from consent.test_purposes import register_physician
from identity.permission_support import owner_context
from identity.sql_denial_contracts import (
    DML_CONTRACTS,
    DmlMember,
    Refusal,
    SqlVerdict,
    _contents,
    probe_dml,
    redeem,
    sql_rowset,
)
from patient_service_support import runtime_role
from renewal.test_consent import accept, seed
from renewal.test_encounters import seed as clinical_seed

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

FUNCTION = "clinic_app.consent_guard"
TEXT_MEMBER = DmlMember(
    FUNCTION, "clinic_app.consent_consenttext", "INSERT", "consent_consenttext"
)
ACCEPTANCE_MEMBER = DmlMember(
    FUNCTION,
    "clinic_app.consent_consentacceptance",
    "INSERT",
    "consent_consentacceptance",
)
REVOCATION_MEMBER = DmlMember(
    FUNCTION,
    "clinic_app.consent_consentrevocation",
    "INSERT",
    "consent_consentrevocation",
)
REFUSAL_MEMBER = DmlMember(
    "clinic_app.consent_guard",
    "clinic_app.consent_refusalrecord",
    "INSERT",
    "consent_refusalrecord",
)
NOTICE_MEMBER = DmlMember(
    "clinic_app.consent_guard",
    "clinic_app.consent_noticeversion",
    "INSERT",
    "consent_noticeversion",
)
DISCLOSURE_MEMBER = DmlMember(
    "clinic_app.consent_guard",
    "clinic_app.consent_aiusedisclosure",
    "INSERT",
    "consent_aiusedisclosure",
)
PARTICIPANT_MEMBER = DmlMember(
    "clinic_app.consent_guard",
    "clinic_app.consent_participantacknowledgment",
    "INSERT",
    "consent_participantacknowledgment",
)
MEMBERS = (
    TEXT_MEMBER,
    ACCEPTANCE_MEMBER,
    REVOCATION_MEMBER,
    REFUSAL_MEMBER,
    NOTICE_MEMBER,
    DISCLOSURE_MEMBER,
    PARTICIPANT_MEMBER,
)
BODY = "Synthetic current-source trigger member"
type ConsentRow = (
    ConsentText
    | ConsentAcceptance
    | ConsentRevocation
    | RefusalRecord
    | NoticeVersion
    | AIUseDisclosure
    | ParticipantAcknowledgment
)


@dataclass(frozen=True)
class World:
    graph: RbacGraph
    text: ConsentText
    session: UUID
    enrollment: UUID
    patient: UUID
    manager: UUID
    other_session: UUID
    other_patient: UUID
    other_receipt: UUID
    encounter: UUID
    clinical_patient: UUID


@pytest.fixture
def dml_world(rbac_graph: RbacGraph) -> World:
    text, session, enrollment, manager = seed(rbac_graph)
    with owner_context(rbac_graph.organization_a):
        patient = PatientClinicEnrollment.objects.get(pk=enrollment).patient_id
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        registration = create_patient(
            clinic_id=rbac_graph.clinic_a,
            full_name="Synthetic second DML patient",
            birth_date=date(1991, 2, 3),
            idempotency_key=uuid4(),
        )
        invitation = issue_invitation(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
        )
    with runtime_role():
        other_session = redeem_invitation(rbac_graph.clinic_a, invitation.secret)
    assert other_session is not None
    with runtime_role(), patient_session_context(other_session):
        other_receipt = accept(text).pk
    appointment, _ = clinical_seed(rbac_graph)
    register_physician(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        encounter = open_encounter(
            clinic_id=rbac_graph.clinic_a, appointment_id=appointment.pk
        )
    return World(
        rbac_graph,
        text,
        session,
        enrollment,
        patient,
        manager,
        other_session,
        registration.patient.pk,
        other_receipt,
        encounter.pk,
        encounter.patient_id,
    )


def _staff_row(member: DmlMember, w: World) -> ConsentRow | None:
    """Construct valid unsaved staff-publication/attestation rows."""
    if member == TEXT_MEMBER:
        return ConsentText(
            organization_id=w.graph.organization_a,
            clinic_id=w.graph.clinic_a,
            purpose="teleconsultation",
            version=w.text.version + 1,
            language="pt-BR",
            text=BODY,
            digest=sha256(BODY.encode()).hexdigest(),
            published_by_id=w.manager,
        )
    if member == NOTICE_MEMBER:
        return NoticeVersion(
            organization_id=w.graph.organization_a,
            clinic_id=w.graph.clinic_a,
            topic="care_processing",
            version=1,
            language="pt-BR",
            text=BODY,
            digest=sha256(BODY.encode()).hexdigest(),
            published_by_id=w.manager,
        )
    if member == REFUSAL_MEMBER:
        return RefusalRecord(
            organization_id=w.graph.organization_a,
            clinic_id=w.graph.clinic_a,
            enrollment_id=w.enrollment,
            patient_id=w.patient,
            text=w.text,
            patient_session_id=w.session,
            refused_at=timezone.now(),
        )
    if member == DISCLOSURE_MEMBER:
        return AIUseDisclosure(
            organization_id=w.graph.organization_a,
            clinic_id=w.graph.clinic_a,
            encounter_id=w.encounter,
            patient_id=w.clinical_patient,
            informed=True,
            refused=False,
            recorded_by_id=w.graph.physician,
            recorded_at=timezone.now(),
        )
    if member == PARTICIPANT_MEMBER:
        return ParticipantAcknowledgment(
            organization_id=w.graph.organization_a,
            clinic_id=w.graph.clinic_a,
            session_id=w.encounter,
            participant_kind="caregiver",
            acknowledged_by_clinician_id=w.graph.physician,
            acknowledged_at=timezone.now(),
        )
    return None


def row_for(member: DmlMember, w: World) -> ConsentRow:
    """Construct valid unsaved rows; prerequisite receipts are genuine fixtures."""
    staff_row = _staff_row(member, w)
    if staff_row is not None:
        return staff_row
    if member == ACCEPTANCE_MEMBER:
        return ConsentAcceptance(
            organization_id=w.graph.organization_a,
            clinic_id=w.graph.clinic_a,
            patient_id=w.patient,
            enrollment_id=w.enrollment,
            text_id=w.text.pk,
            patient_session_id=w.session,
            accepted_at=timezone.now(),
        )
    assert member == REVOCATION_MEMBER
    with runtime_role(), patient_session_context(w.session):
        receipt = accept(w.text)
    return ConsentRevocation(
        organization_id=w.graph.organization_a,
        clinic_id=w.graph.clinic_a,
        acceptance_id=receipt.pk,
        patient_session_id=w.session,
        revoked_at=timezone.now(),
    )


def bind(w: World, member: DmlMember, *, valid: bool) -> None:
    """Reach the DB itself: Python patient/actor guards cannot mask refusal."""
    staff = member in (
        TEXT_MEMBER,
        NOTICE_MEMBER,
        DISCLOSURE_MEMBER,
        PARTICIPANT_MEMBER,
    )
    actor = w.manager if member in (TEXT_MEMBER, NOTICE_MEMBER) else w.graph.physician
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true), "
            "set_config('app.current_user_id', %s, true), "
            "set_config('app.current_patient_session', %s, true)",
            [
                str(w.graph.organization_a) if staff else "",
                str(actor if valid else uuid4()) if staff else "",
                str(w.session if valid else uuid4()) if not staff else "",
            ],
        )


def test_all_current_consent_insert_trigger_members_are_declared() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT n.nspname || '.' || c.relname, c.relname "
            "FROM pg_catalog.pg_trigger t "
            "JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE t.tgfoid='clinic_app.consent_guard()'::regprocedure "
            "AND NOT t.tgisinternal AND (t.tgtype & 7)=7"
        )
        actual = {
            DmlMember(FUNCTION, relation, "INSERT", branch)
            for relation, branch in cursor.fetchall()
        }
    assert actual == set(MEMBERS)
    assert actual == {member for member in DML_CONTRACTS if member.function == FUNCTION}


@pytest.mark.parametrize("member", MEMBERS)
def test_current_members_prove_stored_rows_and_exact_refusals(
    dml_world: World, member: DmlMember
) -> None:
    w = dml_world
    for valid in (True, False):
        row = row_for(member, w)
        with runtime_role(), transaction.atomic():
            bind(w, member, valid=valid)
            verdict = probe_dml(member, row)
            assert verdict.member == member
            if valid:
                assert verdict.observed == sql_rowset([(row.pk,)])
                stored = type(row).objects.get(pk=row.pk)
                assert (stored.organization_id, stored.clinic_id) == (
                    w.graph.organization_a,
                    w.graph.clinic_a,
                )
                if isinstance(stored, ConsentText):
                    assert (stored.text, stored.digest) == (
                        BODY,
                        sha256(BODY.encode()).hexdigest(),
                    )
                elif isinstance(stored, ConsentAcceptance):
                    assert (
                        stored.patient_id,
                        stored.enrollment_id,
                        stored.text_id,
                    ) == (
                        w.patient,
                        w.enrollment,
                        w.text.pk,
                    )
                elif isinstance(stored, RefusalRecord):
                    assert (
                        stored.patient_id,
                        stored.enrollment_id,
                        stored.text_id,
                        stored.patient_session_id,
                    ) == (
                        w.patient,
                        w.enrollment,
                        w.text.pk,
                        w.session,
                    )
                elif isinstance(stored, NoticeVersion):
                    assert (
                        stored.topic,
                        stored.version,
                        stored.published_by_id,
                    ) == (
                        "care_processing",
                        1,
                        w.manager,
                    )
                elif isinstance(stored, AIUseDisclosure):
                    assert (
                        stored.encounter_id,
                        stored.patient_id,
                        stored.informed,
                        stored.refused,
                        stored.recorded_by_id,
                    ) == (
                        w.encounter,
                        w.clinical_patient,
                        True,
                        False,
                        w.graph.physician,
                    )
                elif isinstance(stored, ParticipantAcknowledgment):
                    assert (
                        stored.session_id,
                        stored.participant_kind,
                        stored.acknowledged_by_clinician_id,
                    ) == (
                        w.encounter,
                        "caregiver",
                        w.graph.physician,
                    )
                else:
                    assert isinstance(row, ConsentRevocation)
                    assert isinstance(stored, ConsentRevocation)
                    assert stored.acceptance_id == row.acceptance_id
            else:
                assert verdict.observed == DML_CONTRACTS[member]
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL ROLE clinic_owner")
                    cursor.execute(
                        "SELECT set_config('app.current_tenant', %s, true)",
                        [str(w.graph.organization_a)],
                    )
                    assert not type(row).objects.filter(pk=row.pk).exists()
                    cursor.execute("SET LOCAL ROLE clinic_app")
            assert redeem(verdict, member) is valid
            transaction.set_rollback(True)


@pytest.mark.parametrize("role", UserClinicRole.Role.values)
def test_publication_member_decides_every_current_role(
    dml_world: World, role: str
) -> None:
    w = dml_world
    actor = create_role_actor(w.graph, UserClinicRole.Role(role))
    row = row_for(TEXT_MEMBER, w)
    assert isinstance(row, ConsentText)
    row.published_by_id = actor.pk
    with runtime_role(), transaction.atomic():
        bind(replace(w, manager=actor.pk), TEXT_MEMBER, valid=True)
        verdict = probe_dml(TEXT_MEMBER, row)
        expected = bool(
            BUNDLES_V1[role] & {"configuration.clinic", "configuration.organization"}
        )
        assert redeem(verdict, TEXT_MEMBER) is expected
        if expected:
            assert ConsentText.objects.get(pk=row.pk).published_by_id == actor.pk
        else:
            assert verdict.observed == DML_CONTRACTS[TEXT_MEMBER]
        transaction.set_rollback(True)


@pytest.mark.parametrize("field", ["function", "allowed", "observed", "member"])
def test_issued_contents_cannot_be_forged_after_issuance(
    dml_world: World, field: str
) -> None:
    w = dml_world
    changed: dict[str, object] = {
        "function": "clinic_app.configuration_guard",
        "allowed": False,
        "observed": sql_rowset([(uuid4(),)]),
        "member": replace(TEXT_MEMBER, branch="wrong_branch"),
    }
    with runtime_role(), transaction.atomic():
        bind(w, TEXT_MEMBER, valid=True)
        issued = probe_dml(TEXT_MEMBER, row_for(TEXT_MEMBER, w))
        object.__setattr__(issued, field, changed[field])
        with pytest.raises(AssertionError, match="issued contents"):
            redeem(issued, TEXT_MEMBER)
        with pytest.raises(AssertionError, match="bypassed probe"):
            redeem(issued, TEXT_MEMBER)
        transaction.set_rollback(True)


@pytest.mark.parametrize("in_place", [False, True])
def test_changed_uuid_values_reject_identical_formatting(
    dml_world: World, monkeypatch: pytest.MonkeyPatch, *, in_place: bool
) -> None:
    w = dml_world

    def display_uuid(_value: UUID) -> str:
        return "UUID('same display')"

    monkeypatch.setattr(UUID, "__repr__", display_uuid)
    with runtime_role(), transaction.atomic():
        bind(w, TEXT_MEMBER, valid=True)
        issued = probe_dml(TEXT_MEMBER, row_for(TEXT_MEMBER, w))
        assert isinstance(issued.observed, tuple)
        original = issued.observed[0][0][1]
        assert type(original) is UUID
        original_value = original.int
        display = repr(issued.observed)
        changed = UUID(int=original_value ^ 1)
        if in_place:
            object.__setattr__(original, "int", changed.int)
        else:
            object.__setattr__(issued, "observed", sql_rowset([(changed,)]))
        assert repr(issued.observed) == display
        altered = issued.observed[0][0][1]
        assert type(altered) is UUID
        assert altered.int != original_value
        stored = ConsentText.objects.get(pk=UUID(int=original_value))
        assert stored.pk.int == original_value
        with pytest.raises(AssertionError, match="issued contents"):
            _ = redeem(issued, TEXT_MEMBER)
        with pytest.raises(AssertionError, match="bypassed probe"):
            _ = redeem(issued, TEXT_MEMBER)
        transaction.set_rollback(True)


def test_datetime_snapshot_copies_actual_offset_values() -> None:
    class Offset(timedelta):
        @property
        def seconds(self) -> int:
            return 0

    class OffsetTimezone(tzinfo):
        def utcoffset(self, _value: datetime | None) -> timedelta:
            return Offset(seconds=1)

        def dst(self, _value: datetime | None) -> timedelta:
            return timedelta()

        def tzname(self, _value: datetime | None) -> str:
            return "Synthetic offset"

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL TIME ZONE 'UTC'")
        cursor.execute("SELECT TIMESTAMPTZ '2026-01-02 03:04:05+00'")
        ((original,),) = cursor.fetchall()
    assert type(original) is datetime
    assert original.utcoffset() == timedelta()
    verdict = SqlVerdict(
        "snapshot-fixture", allowed=True, observed=sql_rowset([(original,)])
    )
    captured = _contents(verdict)
    changed = original.replace(tzinfo=OffsetTimezone())
    assert type(changed) is datetime
    assert changed != original
    object.__setattr__(verdict, "observed", sql_rowset([(changed,)]))
    assert _contents(verdict) != captured


def test_dml_seal_consumes_once_and_binds_every_member_component(
    dml_world: World,
) -> None:
    w = dml_world
    mutations: tuple[DmlMember | str, ...] = (
        replace(TEXT_MEMBER, function="clinic_app.configuration_guard"),
        replace(TEXT_MEMBER, relation=ACCEPTANCE_MEMBER.relation),
        replace(TEXT_MEMBER, operation="UPDATE"),
        replace(TEXT_MEMBER, branch=ACCEPTANCE_MEMBER.branch),
        FUNCTION,
    )
    with runtime_role(), transaction.atomic():
        bind(w, TEXT_MEMBER, valid=True)
        for expected in mutations:
            with transaction.atomic():
                issued = probe_dml(TEXT_MEMBER, row_for(TEXT_MEMBER, w))
                with pytest.raises(AssertionError, match="verdict for"):
                    redeem(issued, expected)
                with pytest.raises(AssertionError, match="bypassed probe"):
                    redeem(issued, TEXT_MEMBER)
                transaction.set_rollback(True)
        forged = SqlVerdict(
            FUNCTION,
            allowed=False,
            observed=DML_CONTRACTS[TEXT_MEMBER],
            member=TEXT_MEMBER,
        )
        for bypass in (False, True, forged):
            with pytest.raises(AssertionError, match="bypassed probe"):
                redeem(bypass, TEXT_MEMBER)
        issued = probe_dml(TEXT_MEMBER, row_for(TEXT_MEMBER, w))
        assert redeem(issued, TEXT_MEMBER) is True
        with pytest.raises(AssertionError, match="bypassed probe"):
            redeem(issued, TEXT_MEMBER)
        transaction.set_rollback(True)


def test_malformed_noop_and_unexpected_errors_never_mint_denials(
    dml_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = dml_world
    with runtime_role(), transaction.atomic():
        bind(w, TEXT_MEMBER, valid=True)
        with monkeypatch.context() as patch:
            patch.setattr(ConsentText, "save", lambda *args, **kwargs: None)
            with pytest.raises(AssertionError, match="stored row"):
                probe_dml(TEXT_MEMBER, row_for(TEXT_MEMBER, w))
        malformed = row_for(TEXT_MEMBER, w)
        assert isinstance(malformed, ConsentText)
        malformed.pk = str(uuid4())
        with pytest.raises(AssertionError, match="UUID"):
            probe_dml(TEXT_MEMBER, malformed)
        wrong_relation = row_for(ACCEPTANCE_MEMBER, w)
        with pytest.raises(AssertionError, match="relation"):
            probe_dml(TEXT_MEMBER, wrong_relation)
        for wrong in (
            replace(TEXT_MEMBER, function="clinic_app.absent_guard"),
            replace(TEXT_MEMBER, operation="UPDATE"),
            replace(TEXT_MEMBER, branch="wrong_branch"),
        ):
            with pytest.raises(AssertionError, match="unclassified"):
                probe_dml(wrong, row_for(TEXT_MEMBER, w))
        for wrong_refusal in (
            Refusal("23514", "invalid consent publication."),
            Refusal("42501", "invalid consent publication"),
        ):
            with monkeypatch.context() as patch:
                patch.setitem(DML_CONTRACTS, TEXT_MEMBER, wrong_refusal)
                bind(w, TEXT_MEMBER, valid=False)
                with pytest.raises(DatabaseError):
                    probe_dml(TEXT_MEMBER, row_for(TEXT_MEMBER, w))
        bind(w, TEXT_MEMBER, valid=True)
        duplicate = row_for(TEXT_MEMBER, w)
        duplicate.pk = w.text.pk
        with pytest.raises(AssertionError, match="fresh"):
            probe_dml(TEXT_MEMBER, duplicate)
        transaction.set_rollback(True)


def test_inactive_revoked_and_cross_clinic_publication_refuse(
    dml_world: World,
) -> None:
    w = dml_world
    for state in ("inactive", "revoked", "cross_clinic"):
        row = row_for(TEXT_MEMBER, w)
        assert isinstance(row, ConsentText)
        with runtime_role(), transaction.atomic():
            bind(w, TEXT_MEMBER, valid=True)
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL ROLE clinic_owner")
                if state == "inactive":
                    User.objects.filter(pk=w.manager).update(is_active=False)
                elif state == "revoked":
                    removed, _ = UserClinicRole.objects.filter(
                        clinic_id=w.graph.clinic_a, user_id=w.manager
                    ).delete()
                    assert removed == 1
                else:
                    row.clinic_id = w.graph.clinic_b
                    row.version = 1
                cursor.execute("SET LOCAL ROLE clinic_app")
            verdict = probe_dml(TEXT_MEMBER, row)
            assert verdict.observed == DML_CONTRACTS[TEXT_MEMBER]
            assert redeem(verdict, TEXT_MEMBER) is False
            transaction.set_rollback(True)


@pytest.mark.parametrize(
    "member", [ACCEPTANCE_MEMBER, REFUSAL_MEMBER, REVOCATION_MEMBER]
)
def test_patient_members_refuse_staff_revoked_session_and_crossed_bindings(
    dml_world: World, member: DmlMember
) -> None:
    w = dml_world
    for state in ("staff", "revoked_session", "cross_clinic", "cross_patient"):
        row = row_for(member, w)
        with runtime_role(), transaction.atomic():
            bind(w, member, valid=True)
            with connection.cursor() as cursor:
                if state == "staff":
                    cursor.execute(
                        "SELECT set_config('app.current_user_id', %s, true)",
                        [str(w.manager)],
                    )
                elif state == "revoked_session":
                    cursor.execute("SET LOCAL ROLE clinic_owner")
                    cursor.execute(
                        "SELECT set_config('app.current_tenant', %s, true)",
                        [str(w.graph.organization_a)],
                    )
                    changed = PatientSession.objects.filter(pk=w.session).update(
                        revoked_at=timezone.now()
                    )
                    assert changed == 1
                    cursor.execute("SET LOCAL ROLE clinic_app")
                    cursor.execute("SELECT set_config('app.current_tenant', '', true)")
                elif state == "cross_clinic":
                    row.clinic_id = w.graph.clinic_b
                else:
                    cursor.execute(
                        "SELECT set_config('app.current_patient_session', %s, true)",
                        [str(w.other_session)],
                    )
            verdict = probe_dml(member, row)
            assert verdict.observed == DML_CONTRACTS[member]
            assert redeem(verdict, member) is False
            transaction.set_rollback(True)


@pytest.mark.parametrize(
    "member", [ACCEPTANCE_MEMBER, REFUSAL_MEMBER, REVOCATION_MEMBER]
)
def test_real_cross_patient_record_binding_errors_propagate(
    dml_world: World, member: DmlMember
) -> None:
    w = dml_world
    row = row_for(member, w)
    if isinstance(row, ConsentAcceptance):
        row.patient_id = w.other_patient
        message = "invalid consent binding"
    elif isinstance(row, RefusalRecord):
        row.patient_id = w.other_patient
        message = "invalid refusal binding"
    else:
        assert isinstance(row, ConsentRevocation)
        row.acceptance_id = w.other_receipt
        message = "invalid revocation binding"
    with runtime_role(), transaction.atomic():
        bind(w, member, valid=True)
        with pytest.raises(DatabaseError) as raised:
            probe_dml(member, row)
        cause = raised.value.__cause__
        assert isinstance(cause, psycopg.Error)
        assert type(cause) is psycopg.errors.CheckViolation
        assert cause.sqlstate == "23514"
        assert cause.diag.message_primary == message
        transaction.set_rollback(True)
