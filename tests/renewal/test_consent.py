"""Consent acceptance, replay denial and retained revocation under clinic_app."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from hashlib import sha256
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.consent.forms import AcceptanceForm
from apps.consent.models import ConsentAcceptance, ConsentRevocation, ConsentText
from apps.consent.services import (
    available_texts,
    consent_for_future_use,
    patient_receipts,
    prepare_acceptance,
    publish_text,
    record_consent,
    revoke_consent,
    staff_receipts,
)
from apps.identity.current_context import CurrentActorError
from apps.intake.access import PatientAccessDeniedError
from apps.intake.models import (
    PatientChannelPreference,
    PatientClinicEnrollment,
    PatientSession,
)
from apps.intake.patient_access import (
    PATIENT_SESSION_KEY,
    issue_invitation,
    patient_session_context,
    redeem_invitation,
)
from apps.intake.services import create_patient
from apps.retention.services import patient_released_records, release_version
from apps.tenancy.db import tenant_context
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.test import Client
from django.utils import timezone
from psycopg import sql

from auth.stepup_test_support import verified_request
from otp_test_support import runtime_role as http_runtime_role
from patient_service_support import runtime_role
from renewal.test_encounters import seed as clinical_seed
from renewal.test_encounters import setup_context
from renewal.test_retention import admin, finalized, staff_client

if TYPE_CHECKING:
    from uuid import UUID

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
TEXT = (
    "Texto sintético de teleconsulta.\n\n"
    "Escolha livre e específica; não autoriza marketing."
)
PURPOSE = "teleconsultation"


def seed(graph: RbacGraph) -> tuple[ConsentText, UUID, UUID, UUID]:
    manager = admin(graph)
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        registration = create_patient(
            clinic_id=graph.clinic_a,
            full_name="Paciente Sintético Consentimento",
            birth_date=date(1990, 1, 1),
            idempotency_key=uuid4(),
        )
        invitation = issue_invitation(
            clinic_id=graph.clinic_a, enrollment_id=registration.enrollment.pk
        )
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        text = publish_text(clinic_id=graph.clinic_a, purpose=PURPOSE, text=TEXT)
    with runtime_role():
        session = redeem_invitation(graph.clinic_a, invitation.secret)
    assert session is not None
    return text, session, registration.enrollment.pk, manager.pk


def accept(text: ConsentText) -> ConsentAcceptance:
    _, offer = prepare_acceptance(text_id=text.pk)
    return record_consent(offer=offer, purpose=PURPOSE, accepted=True)


def patient_client(session_id: UUID) -> Client:
    client = Client()
    session = client.session
    session[PATIENT_SESSION_KEY] = str(session_id)
    session.save()
    return client


def test_exact_receipt_revocation_and_future_use(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    text, session, enrollment, manager = seed(graph)
    with runtime_role(), patient_session_context(session):
        assert available_texts()[0].pk == text.pk
        receipt = accept(text)
        assert accept(text).pk == receipt.pk
        before = patient_receipts()[0]
        assert before.text.text == TEXT
        assert before.text.digest == sha256(TEXT.encode()).hexdigest()
        assert before.text.language == "pt-BR"
        assert before.text.version == 1
        assert before.patient_session_id == session
        assert before.authority == "patient_explicit_action"
    with runtime_role(), tenant_context(manager, graph.organization_a):
        active = consent_for_future_use(
            clinic_id=graph.clinic_a, enrollment_id=enrollment, purpose=PURPOSE
        )
        assert active is not None
        assert active.pk == receipt.pk
        assert (
            consent_for_future_use(
                clinic_id=graph.clinic_a, enrollment_id=enrollment, purpose="marketing"
            )
            is None
        )
        assert PatientChannelPreference.objects.count() == 0
    with runtime_role(), patient_session_context(session):
        revocation = revoke_consent(acceptance_id=receipt.pk)
        assert revoke_consent(acceptance_id=receipt.pk).pk == revocation.pk
        after = patient_receipts()[0]
        assert after.accepted_at == before.accepted_at
        assert after.text.text == before.text.text
        assert after.revocation.pk == revocation.pk
        with pytest.raises(ValidationError):
            accept(text)
    with runtime_role(), tenant_context(manager, graph.organization_a):
        assert (
            consent_for_future_use(
                clinic_id=graph.clinic_a, enrollment_id=enrollment, purpose=PURPOSE
            )
            is None
        )
        assert (
            staff_receipts(clinic_id=graph.clinic_a, enrollment_id=enrollment)[0].pk
            == receipt.pk
        )
    with setup_context(graph.organization_a):
        events = list(
            AuditEvent.objects.filter(
                event_type__in=["consent.accepted", "consent.revoked"]
            ).order_by("seq")
        )
        assert len(events) == 2
        assert all(event.actor_user_id == session for event in events)
        assert all(
            set(event.payload) == {"clinic_id", "object_verb"} for event in events
        )


def test_midflow_overlay_denies_stale_acceptance_preserves_old_receipt(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    text, session, enrollment, manager = seed(graph)
    with runtime_role(), patient_session_context(session):
        _, old_offer = prepare_acceptance(text_id=text.pk)
        receipt = accept(text)
    with runtime_role(), tenant_context(manager, graph.organization_a):
        new = publish_text(
            clinic_id=graph.clinic_a, purpose=PURPOSE, text=TEXT + "\nNova versão."
        )
        assert new.version == 2
        assert (
            consent_for_future_use(
                clinic_id=graph.clinic_a, enrollment_id=enrollment, purpose=PURPOSE
            )
            is None
        )
    with runtime_role(), patient_session_context(session):
        with pytest.raises(ValidationError):
            record_consent(offer=old_offer, purpose=PURPOSE, accepted=True)
        with pytest.raises(PatientAccessDeniedError):
            prepare_acceptance(text_id=text.pk)
        assert patient_receipts()[0].pk == receipt.pk
        assert patient_receipts()[0].text.text == TEXT
        assert accept(new).text_id == new.pk


def test_patient_purpose_token_replays_and_staff_impersonation_denied(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    text, session, _, manager = seed(graph)
    with runtime_role(), patient_session_context(session):
        _, token = prepare_acceptance(text_id=text.pk)
        with pytest.raises(PatientAccessDeniedError):
            record_consent(offer=token, purpose="marketing", accepted=True)
        for invalid in ("broken", token + "x", signing.dumps([], salt="consent.offer")):
            with pytest.raises(PatientAccessDeniedError):
                record_consent(offer=invalid, purpose=PURPOSE, accepted=True)
        with pytest.raises(ValidationError):
            record_consent(offer=token, purpose=PURPOSE, accepted=False)
        receipt = accept(text)
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
    with runtime_role(), patient_session_context(other_session):
        with pytest.raises(PatientAccessDeniedError):
            record_consent(offer=token, purpose=PURPOSE, accepted=True)
        with pytest.raises(PatientAccessDeniedError):
            revoke_consent(acceptance_id=receipt.pk)
        assert patient_receipts() == []
    with runtime_role(), tenant_context(manager, graph.organization_a):
        with pytest.raises(PatientAccessDeniedError):
            record_consent(offer=token, purpose=PURPOSE, accepted=True)
        with pytest.raises(PatientAccessDeniedError):
            revoke_consent(acceptance_id=receipt.pk)
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(CurrentActorError),
    ):
        publish_text(clinic_id=graph.clinic_a, purpose=PURPOSE, text=TEXT)
    with (
        runtime_role(),
        tenant_context(manager, graph.organization_a),
        pytest.raises(CurrentActorError),
    ):
        staff_receipts(clinic_id=graph.clinic_b, enrollment_id=other.enrollment.pk)


def test_raw_mutation_deletion_and_binding_forgery_fail_closed(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    text, session, enrollment, manager = seed(graph)
    with runtime_role(), patient_session_context(session):
        receipt = accept(text)
        revocation = revoke_consent(acceptance_id=receipt.pk)
        for model, pk in (
            (ConsentText, text.pk),
            (ConsentAcceptance, receipt.pk),
            (ConsentRevocation, revocation.pk),
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
        with pytest.raises(DatabaseError), transaction.atomic():
            ConsentAcceptance.objects.filter(pk=receipt.pk).update(authority="staff")
    # Even maintenance-role writes cannot rewrite accepted history.
    with setup_context(graph.organization_a):
        with pytest.raises(DatabaseError), transaction.atomic():
            ConsentText.objects.filter(pk=text.pk).update(text="altered")
        assert ConsentAcceptance.objects.filter(pk=receipt.pk).exists()
        assert ConsentRevocation.objects.filter(pk=revocation.pk).exists()
    with (
        runtime_role(),
        tenant_context(manager, graph.organization_a),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        ConsentAcceptance.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            patient_id=receipt.patient_id,
            enrollment_id=enrollment,
            text_id=text.pk,
            patient_session_id=session,
            accepted_at=timezone.now(),
        )
    with (
        runtime_role(),
        patient_session_context(session),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        ConsentAcceptance.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            patient_id=uuid4(),
            enrollment_id=enrollment,
            text_id=text.pk,
            patient_session_id=session,
            accepted_at=timezone.now(),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("purpose", "marketing"),
        ("language", "en-US"),
        ("text", " "),
        ("text", "x" * 20001),
    ],
)
def test_publication_closed_vocabulary(
    rbac_graph: RbacGraph, field: str, value: str
) -> None:
    graph = rbac_graph
    manager = admin(graph)
    fields = {"purpose": PURPOSE, "text": TEXT, "language": "pt-BR", field: value}
    with (
        runtime_role(),
        tenant_context(manager.pk, graph.organization_a),
        pytest.raises(ValidationError),
    ):
        publish_text(clinic_id=graph.clinic_a, **fields)


def test_operation_and_revoked_session_fail_closed(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    _, session, _, _ = seed(graph)
    with setup_context(graph.organization_a):
        PatientSession.objects.filter(pk=session).update(operations=["enrollment_view"])
    with (
        runtime_role(),
        patient_session_context(session),
        pytest.raises(PatientAccessDeniedError),
    ):
        patient_receipts()
    with setup_context(graph.organization_a):
        PatientSession.objects.filter(pk=session).update(revoked_at=timezone.now())
    with runtime_role(), patient_session_context(session) as binding:
        assert binding is None
        with pytest.raises(PatientAccessDeniedError):
            available_texts()


def test_native_http_explicit_action_receipts_staff_and_cache(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    text, session, enrollment, manager = seed(graph)
    assert AcceptanceForm().fields["accepted"].initial is None
    client = patient_client(session)
    with http_runtime_role():
        response = client.get("/patient/consent/")
        assert response.status_code == 200
        assert {"no-store", "private"} <= set(response["Cache-Control"].split(", "))
        read = client.post(
            "/patient/consent/", {"action": "read", "text_id": str(text.pk)}
        )
        token = read.context["form"].initial["offer"]
        assert read.context["form"]["accepted"].value() is None
        assert (
            client.post(
                "/patient/consent/",
                {"action": "accept", "offer": token, "purpose": PURPOSE},
            ).status_code
            == 400
        )
        accepted = client.post(
            "/patient/consent/",
            {"action": "accept", "offer": token, "purpose": PURPOSE, "accepted": "on"},
        )
        assert accepted.status_code == 200
        receipt = accepted.context["receipts"][0]
        assert (
            client.post(
                "/patient/consent/",
                {"action": "delete", "acceptance_id": str(receipt.pk)},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/patient/consent/",
                {"action": "revoke", "acceptance_id": str(receipt.pk)},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/patient/consent/", {"action": "revoke", "acceptance_id": "bad"}
            ).status_code
            == 403
        )
    with staff_client(manager) as staff:
        url = f"/clinics/{graph.clinic_a}/consent/"
        assert staff.get(url).status_code == 200
        viewed = staff.post(
            url, {"action": "receipts", "enrollment_id": str(enrollment)}
        )
        assert viewed.status_code == 200
        assert viewed.context["receipts"][0].text.text == TEXT
        assert staff.post(url, {"action": "accept", "offer": token}).status_code == 403
        assert (
            staff.post(
                url, {"action": "publish", "purpose": PURPOSE, "text": TEXT + "\nNova."}
            ).status_code
            == 200
        )
        assert (
            staff.post(
                url, {"action": "publish", "purpose": PURPOSE, "text": ""}
            ).status_code
            == 400
        )
    with http_runtime_role():
        assert (
            client.post(
                "/patient/consent/",
                {
                    "action": "accept",
                    "offer": token,
                    "purpose": PURPOSE,
                    "accepted": "on",
                },
            ).status_code
            == 409
        )
        assert Client().get("/patient/consent/").status_code == 403


def test_revocation_preserves_released_clinical_history(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    appointment, template = clinical_seed(graph)
    manager = admin(graph)
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = finalized(graph, appointment, template, request)
        release = release_version(clinic_id=graph.clinic_a, version_id=version.pk)
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        enrollment = PatientClinicEnrollment.objects.get(
            clinic_id=graph.clinic_a, patient_id=appointment.patient_id
        )
        invitation = issue_invitation(
            clinic_id=graph.clinic_a, enrollment_id=enrollment.pk
        )
        text = publish_text(clinic_id=graph.clinic_a, purpose=PURPOSE, text=TEXT)
    with runtime_role():
        session = redeem_invitation(graph.clinic_a, invitation.secret)
    assert session is not None
    with runtime_role(), patient_session_context(session):
        before = patient_released_records()
        assert len(before) == 1
        receipt = accept(text)
        revoke_consent(acceptance_id=receipt.pk)
        assert patient_released_records() == before
    with setup_context(graph.organization_a):
        version.refresh_from_db()
        release.refresh_from_db()
        assert version.state == "finalized"
        assert release.revoked_at is None


def test_parallel_publication_allocates_distinct_versions(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    _, _, _, manager = seed(graph)
    barrier = Barrier(2, timeout=10)

    def publish_parallel() -> int:
        try:
            barrier.wait()
            with runtime_role(), tenant_context(manager, graph.organization_a):
                return publish_text(
                    clinic_id=graph.clinic_a, purpose=PURPOSE, text=TEXT
                ).version
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish_parallel) for _ in range(2)]
        versions = [future.result(timeout=20) for future in futures]
    assert sorted(versions) == [2, 3]


def test_consent_force_rls_and_no_destructive_runtime_privileges(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    text, session, _, _ = seed(graph)
    with runtime_role(), patient_session_context(session):
        accept(text)
    tables = [
        "consent_consenttext",
        "consent_consentacceptance",
        "consent_consentrevocation",
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
    with runtime_role():
        assert ConsentText.objects.count() == 0
        assert ConsentAcceptance.objects.count() == 0
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_b):
        assert ConsentText.objects.count() == 0
        assert ConsentAcceptance.objects.count() == 0


def test_consent_resolvers_have_exact_hardened_catalog_posture() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT p.proname, p.proowner::regrole::text, p.prosecdef, "
            "p.provolatile, p.proconfig, "
            "has_function_privilege('clinic_app',p.oid,'EXECUTE'), "
            "NOT EXISTS (SELECT 1 FROM aclexplode(p.proacl) a "
            "WHERE a.grantee=0 AND a.privilege_type='EXECUTE') "
            "FROM pg_proc p WHERE p.pronamespace='clinic_app'::regnamespace "
            "AND p.proname LIKE 'consent_%%'"
        )
        rows = {row[0]: row[1:] for row in cursor.fetchall()}
    posture = {
        "consent_session": ("clinic_resolver", "s", True),
        "consent_immutable": ("clinic_resolver", "v", False),
        "consent_guard": ("clinic_resolver", "v", False),
        "consent_audit_scope": ("clinic_resolver", "s", False),
        "consent_audit": ("clinic_owner", "v", True),
    }
    assert rows == {
        name: (
            owner,
            True,
            volatility,
            ["search_path=pg_catalog, clinic_app, pg_temp"],
            app_execute,
            True,
        )
        for name, (owner, volatility, app_execute) in posture.items()
    }
