"""Delegate authority is enforced by real PostgreSQL roles, not the client."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from re import search
from threading import Event
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.audit.services import verify_chain
from apps.consent.models import ConsentText
from apps.consent.services import prepare_acceptance, publish_text, record_consent
from apps.ehr.finalization import amend_document, finalize_version
from apps.ehr.services import record_clinical_note
from apps.identity.delegates import (
    DelegateConflictError,
    DelegateGrantRequest,
    DelegateInputError,
    activate,
    request_delegate_grant,
    revoke,
    switch_delegate,
    verify_delegate_grant,
)
from apps.identity.models import PatientDelegateGrant
from apps.intake.access import PatientAccessDeniedError
from apps.intake.models import PATIENT_OPERATION_VALUES, PatientSession
from apps.intake.patient_access import (
    PATIENT_SESSION_KEY,
    patient_session_context,
    patient_session_overview,
)
from apps.retention.services import patient_released_records, release_version
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, connections, transaction
from django.test import Client
from django.utils.translation import gettext

from auth.stepup_test_support import verified_request
from identity.permission_support import permission_context
from patient_service_support import runtime_role
from patients.delegate_support import (
    active_grant,
    delegate_clock,
    delegate_world,
    request_grant,
)
from renewal.test_amendments import AMENDED
from renewal.test_encounters import setup_context
from renewal.test_patient_access import _issue, _redeem, _seed_patients

if TYPE_CHECKING:
    from pathlib import Path

    from apps.identity.delegates import DelegateReceipt

    from patients.delegate_support import DelegateWorld
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_patient_authority_is_a_database_operation_boundary(
    rbac_graph: RbacGraph,
) -> None:
    access = _seed_patients(rbac_graph)
    _, secret = _issue(rbac_graph, access.enrollment_id)
    session = _redeem(rbac_graph.clinic_a, secret)
    assert session is not None
    with (
        runtime_role(),
        patient_session_context(session),
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("clinic_app",)
        cursor.execute(
            "SELECT session_id,enrollment_id FROM "
            "clinic_app.current_patient_authority('enrollment_view')"
        )
        assert cursor.fetchone() == (session, access.enrollment_id)


def test_delegate_database_shape_is_exact_and_runtime_cannot_write_bindings() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity,relforcerowsecurity,relowner::regrole::text "
            "FROM pg_class WHERE "
            "oid='clinic_app.identity_patientdelegategrant'::regclass"
        )
        assert cursor.fetchone() == (True, True, "clinic_owner")
        cursor.execute(
            "SELECT policyname,roles,cmd,permissive FROM pg_policies "
            "WHERE schemaname='clinic_app' AND "
            "tablename='identity_patientdelegategrant'"
        )
        assert {
            (name, tuple(roles), command, mode)
            for name, roles, command, mode in cursor.fetchall()
        } == {
            ("setup_tenant", ("clinic_owner",), "ALL", "PERMISSIVE"),
            ("delegate_read", ("clinic_app",), "SELECT", "PERMISSIVE"),
        }
        for column in ("acting_for_enrollment_id", "delegate_grant_id"):
            cursor.execute(
                "SELECT has_column_privilege('clinic_app',"
                "'clinic_app.intake_patientsession',%s,'UPDATE')",
                [column],
            )
            assert cursor.fetchone() == (False,)
        cursor.execute(
            "SELECT proowner::regrole::text,prosecdef,proconfig,"
            "has_function_privilege('clinic_app',oid,'EXECUTE'),"
            "NOT EXISTS(SELECT 1 FROM aclexplode(proacl) a WHERE a.grantee=0)"
            " FROM pg_proc WHERE oid="
            "'clinic_app.delegate_audit(uuid,text,timestamptz,bytea)'::regprocedure"
        )
        assert cursor.fetchone() == (
            "clinic_owner",
            True,
            ["search_path=pg_catalog, clinic_app, pg_temp"],
            True,
            True,
        )


def _switch(graph: RbacGraph, world: DelegateWorld, grant: DelegateReceipt) -> None:
    with runtime_role(), patient_session_context(world.session_id):
        switch_delegate(clinic_id=graph.clinic_a, grant_id=grant.grant_id)


def test_delegate_native_authority_refuses_inactive_staff_with_patient_context(
    rbac_graph: RbacGraph,
    tmp_path: Path,
) -> None:
    """Neither command resolvers nor the owner trigger inherit patient authority."""
    world = delegate_world(rbac_graph, tmp_path)
    grants = [request_grant(rbac_graph, world) for _ in range(3)]
    native_template = request_grant(rbac_graph, world)
    _, secret = _issue(rbac_graph, world.notes.enrollment_id)
    grantor = _redeem(rbac_graph.clinic_a, secret)
    assert grantor is not None
    outcomes = []
    for active, pending in zip((True, False, True), grants, strict=True):
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE clinic_app.identity_user SET is_active=%s WHERE id=%s",
                [active, world.verifier_id],
            )
        params = (
            rbac_graph.clinic_a,
            world.notes.enrollment_id,
            world.delegate_enrollment_id,
            PatientDelegateGrant.AuthorityKind.GUARDIAN,
            world.evidence_id,
            ["care_processing"],
            ["booking"],
            world.deadline,
            uuid4(),
        )
        for owner, statement, arguments in (
            (
                False,
                "SELECT * FROM clinic_app.request_delegate_grant("
                "%s,%s,%s,%s,%s,%s::text[],%s::text[],%s,%s)",
                params,
            ),
            (
                False,
                "SELECT * FROM clinic_app.transition_delegate_grant(%s,%s,%s,%s)",
                (rbac_graph.clinic_a, pending.grant_id, pending.revision, "verified"),
            ),
            (
                True,
                "INSERT INTO clinic_app.identity_patientdelegategrant "
                "SELECT (jsonb_populate_record("
                "NULL::clinic_app.identity_patientdelegategrant, to_jsonb(g)||"
                "jsonb_build_object('id',%s::uuid,'idempotency_key',%s::uuid))).* "
                "FROM clinic_app.identity_patientdelegategrant g WHERE g.id=%s",
                (uuid4(), uuid4(), native_template.grant_id),
            ),
        ):
            with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
                if owner:
                    cursor.execute("SET LOCAL ROLE clinic_owner")
                cursor.execute(
                    "SELECT set_config('app.current_tenant',%s,true),"
                    "set_config('app.current_user_id',%s,true),"
                    "set_config('app.current_patient_session',%s,true)",
                    [
                        str(rbac_graph.organization_a),
                        str(world.verifier_id),
                        str(grantor),
                    ],
                )
                try:
                    with transaction.atomic():
                        cursor.execute(statement, arguments)
                except DatabaseError as error:
                    outcomes.append(getattr(error.__cause__, "sqlstate", None))
                else:
                    outcomes.append(True)
    assert outcomes == [True, True, True, "42501", "42501", "42501", True, True, True]


def test_delegate_sm_verification_idempotency_and_stale_commands(
    rbac_graph: RbacGraph,
    tmp_path: Path,
) -> None:
    world = delegate_world(rbac_graph, tmp_path)
    key = uuid4()
    pending = request_grant(rbac_graph, world, key=key)
    assert pending.state == "pending_verification"
    assert request_grant(rbac_graph, world, key=key) == pending
    with permission_context(rbac_graph, world.verifier_id):
        with pytest.raises(DelegateConflictError, match="delegate conflict"):
            activate(
                clinic_id=rbac_graph.clinic_a,
                grant_id=pending.grant_id,
                expected_revision=pending.revision,
            )
        verified = verify_delegate_grant(
            clinic_id=rbac_graph.clinic_a,
            grant_id=pending.grant_id,
            expected_revision=pending.revision,
            verified=True,
        )
        assert verified.revision == 2
        assert (
            verify_delegate_grant(
                clinic_id=rbac_graph.clinic_a,
                grant_id=pending.grant_id,
                expected_revision=pending.revision,
                verified=True,
            )
            == verified
        )
        with pytest.raises(DelegateConflictError):
            activate(
                clinic_id=rbac_graph.clinic_a,
                grant_id=pending.grant_id,
                expected_revision=pending.revision,
            )
        active = activate(
            clinic_id=rbac_graph.clinic_a,
            grant_id=pending.grant_id,
            expected_revision=verified.revision,
        )
        assert active.state == "active"
    with (
        runtime_role(),
        patient_session_context(world.session_id),
        pytest.raises(PatientAccessDeniedError),
    ):
        verify_delegate_grant(
            clinic_id=rbac_graph.clinic_a,
            grant_id=active.grant_id,
            expected_revision=active.revision,
            verified=True,
        )
    with setup_context(rbac_graph.organization_a):
        stored = PatientDelegateGrant.objects.get(pk=active.grant_id)
        assert stored.verified_by_id == world.verifier_id
        assert stored.evidence_attachment_id == world.evidence_id
        events = list(
            AuditEvent.objects.filter(event_type__startswith="identity.delegate.")
        )
        assert [event.event_type for event in events] == [
            "identity.delegate.requested",
            "identity.delegate.verified",
            "identity.delegate.activated",
        ]
        assert all(
            set(event.payload) == {"clinic_id", "object_verb"} for event in events
        )
        assert verify_chain(rbac_graph.organization_a).valid


@pytest.mark.parametrize("kind", PatientDelegateGrant.AuthorityKind.values)
def test_rejected_evidence_never_activates(
    rbac_graph: RbacGraph,
    tmp_path: Path,
    kind: str,
) -> None:
    world = delegate_world(rbac_graph, tmp_path)
    with permission_context(rbac_graph, world.verifier_id):
        pending = request_delegate_grant(
            clinic_id=rbac_graph.clinic_a,
            grant=DelegateGrantRequest(
                represented_enrollment_id=world.notes.enrollment_id,
                delegate_enrollment_id=world.delegate_enrollment_id,
                authority_kind=kind,
                evidence_attachment_id=world.evidence_id,
                purposes=("care_processing",),
                operations=("enrollment_view", "booking"),
                expires_at=world.deadline,
                idempotency_key=uuid4(),
            ),
        )
    with permission_context(rbac_graph, world.verifier_id):
        rejected = verify_delegate_grant(
            clinic_id=rbac_graph.clinic_a,
            grant_id=pending.grant_id,
            expected_revision=pending.revision,
            verified=False,
        )
        with pytest.raises(DelegateConflictError):
            activate(
                clinic_id=rbac_graph.clinic_a,
                grant_id=rejected.grant_id,
                expected_revision=rejected.revision,
            )
    with (
        runtime_role(),
        patient_session_context(world.session_id),
        pytest.raises(PatientAccessDeniedError),
    ):
        switch_delegate(clinic_id=rbac_graph.clinic_a, grant_id=rejected.grant_id)


def test_delegate_scope_is_stored_and_cannot_widen_or_cross_clinic(
    rbac_graph: RbacGraph,
    tmp_path: Path,
) -> None:
    world = delegate_world(rbac_graph, tmp_path)
    grant = active_grant(rbac_graph, world, operations=("enrollment_view", "booking"))
    _switch(rbac_graph, world, grant)
    with runtime_role(), patient_session_context(world.session_id) as binding:
        assert binding is not None
        assert binding.enrollment_id == world.notes.enrollment_id
        assert binding.acting_for_enrollment_id == world.notes.enrollment_id
        assert binding.delegate_grant_id == grant.grant_id
        assert binding.operations == ("enrollment_view", "booking")
        overview = patient_session_overview()
        assert overview is not None
        assert overview.operations == binding.operations
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT enrollment_id FROM clinic_app.patient_booking_scope()"
            )
            assert cursor.fetchone() == (world.notes.enrollment_id,)
            cursor.execute("SELECT * FROM clinic_app.retention_records_session()")
            assert cursor.fetchall() == []
            cursor.execute("SELECT * FROM clinic_app.consent_session()")
            assert cursor.fetchall() == []
            cursor.execute("SELECT clinic_app.questionnaire_patient_enrollment()")
            assert cursor.fetchone() == (None,)
            for table in ("identity_patientdelegategrant", "intake_patientsession"):
                cursor.execute(
                    "SELECT has_table_privilege('clinic_app',%s,'INSERT'),"
                    "has_table_privilege('clinic_app',%s,'UPDATE'),"
                    "has_table_privilege('clinic_app',%s,'DELETE')",
                    [f"clinic_app.{table}"] * 3,
                )
                assert cursor.fetchone() == (False, False, False)
            with pytest.raises(DatabaseError), transaction.atomic():
                cursor.execute(
                    "UPDATE clinic_app.identity_patientdelegategrant "
                    "SET operations=%s WHERE id=%s",
                    [list(PATIENT_OPERATION_VALUES), grant.grant_id],
                )
        for clinic, selected in (
            (rbac_graph.clinic_b, grant.grant_id),
            (rbac_graph.clinic_a, uuid4()),
        ):
            with pytest.raises(PatientAccessDeniedError, match="patient access denied"):
                switch_delegate(clinic_id=clinic, grant_id=selected)
    with setup_context(rbac_graph.organization_a):
        raw = PatientSession.objects.get(pk=world.session_id)
        assert raw.enrollment_id == world.delegate_enrollment_id
        assert raw.patient_id != world.notes.patient_id
        for updates in (
            {"created_at": raw.created_at + timedelta(seconds=1)},
            {"expires_at": raw.expires_at + timedelta(seconds=1)},
        ):
            with pytest.raises(DatabaseError), transaction.atomic():
                PatientSession.objects.filter(pk=raw.pk).update(**updates)


def test_restricted_records_are_hidden_in_database_and_unknown_denials_match(
    rbac_graph: RbacGraph,
    tmp_path: Path,
) -> None:
    world = delegate_world(rbac_graph, tmp_path)
    request = verified_request(rbac_graph.physician)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        original = finalize_version(
            clinic_id=rbac_graph.clinic_a,
            version_id=world.notes.first_version_id,
            expected_revision=2,
            request=request,
        )
        amendment = amend_document(
            clinic_id=rbac_graph.clinic_a,
            version_id=original.pk,
            reason="Synthetic confidentiality acceptance",
        )
        saved = record_clinical_note(
            clinic_id=rbac_graph.clinic_a,
            version_id=amendment.pk,
            expected_revision=1,
            content=AMENDED,
        )
        amended = finalize_version(
            clinic_id=rbac_graph.clinic_a,
            version_id=saved.pk,
            expected_revision=2,
            request=request,
        )
        restricted = release_version(
            clinic_id=rbac_graph.clinic_a, version_id=original.pk
        )
        shared = release_version(
            clinic_id=rbac_graph.clinic_a,
            version_id=amended.pk,
            restricted_from_delegates=False,
        )
    grant = active_grant(rbac_graph, world)
    _switch(rbac_graph, world, grant)
    with runtime_role(), patient_session_context(world.session_id):
        assert [record.version_id for record in patient_released_records()] == [
            shared.version_id
        ]
        with connection.cursor() as cursor:
            denials = []
            for selected in (restricted.version_id, uuid4()):
                cursor.execute(
                    "SELECT * FROM clinic_app.retention_patient_releases() WHERE "
                    "version_id=%s",
                    [selected],
                )
                denials.append(cursor.fetchall())
            assert denials == [[], []]
    client = Client()
    session = client.session
    session[PATIENT_SESSION_KEY] = str(world.session_id)
    session.save()
    with runtime_role():
        attempts = [
            client.post(
                "/patient/records/", {"action": "download", "version_id": str(selected)}
            )
            for selected in (restricted.version_id, uuid4())
        ]
    assert [response.status_code for response in attempts] == [403, 403]
    assert attempts[0].content == attempts[1].content


def test_delegate_consent_keeps_exact_purpose_and_truthful_authority(
    rbac_graph: RbacGraph,
    tmp_path: Path,
) -> None:
    world = delegate_world(rbac_graph, tmp_path)
    with permission_context(rbac_graph, world.verifier_id):
        allowed = publish_text(
            clinic_id=rbac_graph.clinic_a,
            purpose="teleconsultation",
            text="Synthetic exact purpose text",
        )
        forbidden = publish_text(
            clinic_id=rbac_graph.clinic_a,
            purpose="marketing",
            text="Synthetic optional purpose text",
        )
    grant = active_grant(
        rbac_graph,
        world,
        operations=("enrollment_view", "consent"),
        purposes=("teleconsultation",),
    )
    _switch(rbac_graph, world, grant)
    with runtime_role(), patient_session_context(world.session_id):
        assert list(ConsentText.objects.values_list("pk", flat=True)) == [allowed.pk]
        _, offer = prepare_acceptance(text_id=allowed.pk)
        acceptance = record_consent(
            offer=offer, purpose="teleconsultation", accepted=True
        )
        acceptance.refresh_from_db()
        assert acceptance.patient_id == world.notes.patient_id
        assert acceptance.patient_session_id == world.session_id
        assert acceptance.authority == "delegate_explicit_action"
        with pytest.raises(ConsentText.DoesNotExist):
            ConsentText.objects.get(pk=forbidden.pk)


def test_revoke_and_expiry_deny_next_request_and_renewal_stays_pending(
    rbac_graph: RbacGraph,
    tmp_path: Path,
) -> None:
    world = delegate_world(rbac_graph, tmp_path)
    grant = active_grant(rbac_graph, world)
    _switch(rbac_graph, world, grant)
    client = Client()
    session = client.session
    session[PATIENT_SESSION_KEY] = str(world.session_id)
    session.save()
    with runtime_role():
        assert client.get("/patient/").status_code == 200
    # Equality is expired too; advancing past the boundary would miss >= bugs.
    with delegate_clock(world.deadline):
        with runtime_role():
            expired = client.get("/patient/")
        assert expired.status_code == 403
        assert gettext("Representation access expired").encode() in expired.content
        with runtime_role(), patient_session_context(world.session_id) as binding:
            assert binding is None
        match = search(r'name="offer" value="([^"]+)"', expired.content.decode())
        assert match is not None
        offer = match[1]
        with runtime_role():
            renewal = client.post("/patient/delegation/renew/", {"offer": offer})
            assert renewal.status_code == 200
            replay = client.post("/patient/delegation/renew/", {"offer": offer})
            assert replay.status_code == 200
            assert client.get("/patient/").status_code == 403
        with setup_context(rbac_graph.organization_a):
            renewed = PatientDelegateGrant.objects.get(renewal_of_id=grant.grant_id)
            assert renewed.state == "pending_verification"
            assert renewed.operations == [
                "enrollment_view",
                "booking",
                "records",
                "consent",
            ]
            assert renewed.verified_by_id is None
            assert (
                PatientDelegateGrant.objects.filter(
                    renewal_of_id=grant.grant_id
                ).count()
                == 1
            )
            assert verify_chain(rbac_graph.organization_a).valid
    with permission_context(rbac_graph, world.verifier_id):
        revoke(
            clinic_id=rbac_graph.clinic_a,
            grant_id=grant.grant_id,
            expected_revision=grant.revision,
        )
    with runtime_role():
        assert client.get("/patient/").status_code == 403
        assert (
            client.post("/patient/delegation/renew/", {"offer": offer}).status_code
            == 403
        )


@pytest.mark.parametrize("operations", [(), ("admin",), ("records", None), "records"])
def test_malformed_scope_cannot_create_authority(
    rbac_graph: RbacGraph,
    tmp_path: Path,
    operations: tuple[str, ...],
) -> None:
    world = delegate_world(rbac_graph, tmp_path)
    with (
        permission_context(rbac_graph, world.verifier_id),
        pytest.raises(DelegateInputError),
    ):
        request_delegate_grant(
            clinic_id=rbac_graph.clinic_a,
            grant=DelegateGrantRequest(
                represented_enrollment_id=world.notes.enrollment_id,
                delegate_enrollment_id=world.delegate_enrollment_id,
                authority_kind="legal_guardian_minor",
                evidence_attachment_id=world.evidence_id,
                purposes=("care_processing",),
                operations=operations,
                expires_at=world.deadline,
                idempotency_key=uuid4(),
            ),
        )
    with setup_context(rbac_graph.organization_a):
        assert PatientDelegateGrant.objects.count() == 0


def test_committed_revocation_denies_a_previously_bound_open_request(
    rbac_graph: RbacGraph,
    tmp_path: Path,
) -> None:
    world = delegate_world(rbac_graph, tmp_path)
    grant = active_grant(rbac_graph, world)
    _switch(rbac_graph, world, grant)
    committed = Event()

    def revoke_bound() -> None:
        try:
            with permission_context(rbac_graph, world.verifier_id):
                revoke(
                    clinic_id=rbac_graph.clinic_a,
                    grant_id=grant.grant_id,
                    expected_revision=grant.revision,
                )
        finally:
            connections["default"].close()
            committed.set()

    with (
        ThreadPoolExecutor(max_workers=1) as workers,
        runtime_role(),
        patient_session_context(world.session_id) as binding,
    ):
        assert binding is not None
        future = workers.submit(revoke_bound)
        assert committed.wait(timeout=10)
        future.result(timeout=10)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM clinic_app.current_patient_authority('records')"
            )
            assert cursor.fetchall() == []
            cursor.execute("SELECT * FROM clinic_app.patient_booking_scope()")
            assert cursor.fetchall() == []
