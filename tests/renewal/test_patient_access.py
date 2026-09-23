"""Acceptance tests for enrollment-bound patient invitations and sessions.

Covers the staff issue/revoke services, the resolver-owned redemption and
session functions, the patient request boundary in the tenant middleware,
and the POST-only screens. Codes and names are synthetic; the raw secret
is never persisted and never reaches a URL.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.intake.models import (
    PATIENT_OPERATION_VALUES,
    Patient,
    PatientAccessGrant,
    PatientSession,
)
from apps.intake.patient_access import (
    PATIENT_SESSION_KEY,
    end_patient_session,
    patient_session_context,
    patient_session_overview,
    redeem_invitation,
)
from apps.intake.services import (
    PatientAccessDeniedError,
    access_overview,
    create_patient,
    issue_invitation,
    revoke_patient_access,
)
from apps.tenancy.db import clear_connection_tenant_gucs, tenant_context
from django.db import connection, transaction
from django.test import Client
from django.utils import timezone
from django.utils.translation import gettext

from otp_test_support import OTP_RAW_CREDENTIAL, create_receptionist, runtime_role
from patient_service_support import runtime_role as service_runtime_role

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

INVALID_CODE_MESSAGE = gettext(
    "This access code is not valid. Check the code or ask the clinic for a "
    "new invitation."
)


@dataclass(frozen=True, slots=True)
class AccessGraph:
    """One enrollment in clinic A plus a second enrollment in clinic B."""

    enrollment_id: UUID
    patient_id: UUID
    other_enrollment_id: UUID
    other_patient_id: UUID


def _seed_patients(graph: RbacGraph) -> AccessGraph:
    with (
        service_runtime_role(),
        tenant_context(graph.shared_user, graph.organization_a),
    ):
        first = create_patient(
            clinic_id=graph.clinic_a,
            full_name="Ana Synthetic Access",
            birth_date=date(1990, 5, 17),
            idempotency_key=uuid4(),
        )
    with (
        service_runtime_role(),
        tenant_context(graph.clinic_admin, graph.organization_a),
    ):
        second = create_patient(
            clinic_id=graph.clinic_b,
            full_name="Bia Synthetic Access",
            birth_date=date(1985, 3, 9),
            idempotency_key=uuid4(),
        )
    return AccessGraph(
        enrollment_id=first.enrollment.pk,
        patient_id=first.patient.pk,
        other_enrollment_id=second.enrollment.pk,
        other_patient_id=second.patient.pk,
    )


def _as_manager(graph: RbacGraph) -> AbstractContextManager[None]:
    return tenant_context(graph.shared_user, graph.organization_a)


def _issue(graph: RbacGraph, enrollment_id: UUID) -> tuple[UUID, str]:
    with service_runtime_role(), _as_manager(graph):
        issued = issue_invitation(
            clinic_id=graph.clinic_a,
            enrollment_id=enrollment_id,
        )
    return issued.grant.pk, issued.secret


def _redeem(clinic_id: UUID, code: str) -> UUID | None:
    with runtime_role():
        return redeem_invitation(clinic_id, code)


def _grants(
    organization_id: UUID,
    **lookup: object,
) -> list[PatientAccessGrant]:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        return list(PatientAccessGrant.objects.filter(**lookup))


def _sessions(
    organization_id: UUID,
    **lookup: object,
) -> list[PatientSession]:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        return list(PatientSession.objects.filter(**lookup))


def _grant(grant_id: UUID, organization_id: UUID) -> PatientAccessGrant:
    rows = _grants(organization_id, pk=grant_id)
    assert len(rows) == 1
    return rows[0]


def _session(session_id: UUID, organization_id: UUID) -> PatientSession:
    rows = _sessions(organization_id, pk=session_id)
    assert len(rows) == 1
    return rows[0]


def _expire_grant(grant_id: UUID, organization_id: UUID) -> None:
    """Move the grant's lifetime into the past inside its own CHECK."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        PatientAccessGrant.objects.filter(pk=grant_id).update(
            created_at=timezone.now() - timedelta(hours=25),
            expires_at=timezone.now() - timedelta(hours=1),
        )


def _age_session(
    session_id: UUID,
    organization_id: UUID,
    *,
    idle: timedelta | None = None,
    absolute: timedelta | None = None,
) -> None:
    """Move session deadlines into the past inside the expiry CHECK."""
    updates: dict[str, object] = {}
    if idle is not None:
        updates["idle_expires_at"] = timezone.now() - idle
    if absolute is not None:
        updates["created_at"] = timezone.now() - timedelta(hours=9)
        updates["expires_at"] = timezone.now() - absolute
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        PatientSession.objects.filter(pk=session_id).update(**updates)


def _patient_client(session_id: UUID) -> Client:
    client = Client()
    session = client.session
    session[PATIENT_SESSION_KEY] = str(session_id)
    session.save()
    return client


def test_issue_stores_only_the_hash_and_binds_the_enrollment(
    rbac_graph: RbacGraph,
) -> None:
    access = _seed_patients(rbac_graph)
    grant_id, secret = _issue(rbac_graph, access.enrollment_id)

    grant = _grant(grant_id, rbac_graph.organization_a)
    assert bytes(grant.secret_hash) == hashlib.sha256(secret.encode()).digest()
    assert grant.enrollment_id == access.enrollment_id
    assert grant.patient_id == access.patient_id
    assert grant.clinic_id == rbac_graph.clinic_a
    assert grant.organization_id == rbac_graph.organization_a
    assert list(grant.operations) == PATIENT_OPERATION_VALUES
    assert grant.consumed_at is None
    assert grant.revoked_at is None
    assert (
        timedelta(hours=23) < grant.expires_at - grant.created_at <= timedelta(hours=24)
    )


def test_redeem_consumes_once_and_binds_server_side(
    rbac_graph: RbacGraph,
) -> None:
    access = _seed_patients(rbac_graph)
    grant_id, secret = _issue(rbac_graph, access.enrollment_id)

    session_id = _redeem(rbac_graph.clinic_a, secret)
    assert session_id is not None

    grant = _grant(grant_id, rbac_graph.organization_a)
    assert grant.consumed_at is not None

    session = _session(session_id, rbac_graph.organization_a)
    assert session.grant_id == grant_id
    assert session.organization_id == rbac_graph.organization_a
    assert session.clinic_id == rbac_graph.clinic_a
    assert session.patient_id == access.patient_id
    assert session.enrollment_id == access.enrollment_id
    assert list(session.operations) == PATIENT_OPERATION_VALUES
    assert session.revoked_at is None
    assert (
        timedelta(hours=7)
        < session.expires_at - session.created_at
        <= timedelta(hours=8)
    )
    assert session.idle_expires_at - session.created_at <= timedelta(minutes=30)

    # Single-use: the second redemption of the same code fails identically.
    assert _redeem(rbac_graph.clinic_a, secret) is None


def test_redeem_rejects_unknown_expired_revoked_and_wrong_clinic(
    rbac_graph: RbacGraph,
) -> None:
    access = _seed_patients(rbac_graph)
    grant_id, secret = _issue(rbac_graph, access.enrollment_id)

    # Unknown code and malformed input fail closed.
    assert _redeem(rbac_graph.clinic_a, "not-a-real-code") is None
    assert _redeem(rbac_graph.clinic_a, "") is None
    assert _redeem(uuid4(), secret) is None

    # A code issued for clinic A fails at every other redemption path.
    assert _redeem(rbac_graph.clinic_b, secret) is None
    assert _redeem(rbac_graph.clinic_c, secret) is None

    # Expired invitations cannot be redeemed.
    _expire_grant(grant_id, rbac_graph.organization_a)
    assert _redeem(rbac_graph.clinic_a, secret) is None

    # A revoked invitation cannot be redeemed.
    second_grant_id, second_secret = _issue(rbac_graph, access.enrollment_id)
    with service_runtime_role(), _as_manager(rbac_graph):
        revoke_patient_access(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=access.enrollment_id,
            grant_id=second_grant_id,
        )
    assert _redeem(rbac_graph.clinic_a, second_secret) is None


def test_session_touch_validates_and_renews_idle_deadline(
    rbac_graph: RbacGraph,
) -> None:
    access = _seed_patients(rbac_graph)
    _, secret = _issue(rbac_graph, access.enrollment_id)
    session_id = _redeem(rbac_graph.clinic_a, secret)
    assert session_id is not None

    with runtime_role(), patient_session_context(session_id) as binding:
        assert binding is not None
        assert binding.session_id == session_id
        assert binding.organization_id == rbac_graph.organization_a
        assert binding.clinic_id == rbac_graph.clinic_a
        assert binding.patient_id == access.patient_id
        assert binding.enrollment_id == access.enrollment_id
        assert binding.operations == tuple(PATIENT_OPERATION_VALUES)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_setting('app.current_patient_session', true), "
                "current_setting('app.current_tenant', true), "
                "current_setting('app.current_user_id', true)"
            )
            row = cursor.fetchone()
        assert row is not None
        patient_guc, tenant_guc, user_guc = row
        assert patient_guc == str(session_id)
        assert tenant_guc in (None, "")
        assert user_guc in (None, "")

    # The touch renewed the idle deadline and the patient GUC is gone.
    renewed = _session(session_id, rbac_graph.organization_a)
    assert renewed.idle_expires_at > timezone.now() + timedelta(minutes=29)
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.current_patient_session', true)")
        row = cursor.fetchone()
    assert row is not None
    assert row[0] in (None, "")

    # An unknown session id yields no binding.
    with runtime_role(), patient_session_context(uuid4()) as binding:
        assert binding is None


def test_patient_context_clears_persistent_staff_gucs(
    rbac_graph: RbacGraph,
) -> None:
    """A dirty reused connection cannot carry staff authority into patient work.

    Persistent ``app.current_tenant``/``app.current_user_id`` set before the
    boundary must not survive inside it: the patient context clears them at
    entry so tenant-isolation policies stay fail closed.
    """
    access = _seed_patients(rbac_graph)
    _, secret = _issue(rbac_graph, access.enrollment_id)
    session_id = _redeem(rbac_graph.clinic_a, secret)
    assert session_id is not None

    try:
        with runtime_role():
            # Persistent (non-transactional) staff GUCs on the same connection.
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_catalog.set_config('app.current_tenant', %s, false), "
                    "pg_catalog.set_config('app.current_user_id', %s, false)",
                    [str(rbac_graph.organization_a), str(rbac_graph.shared_user)],
                )
            with patient_session_context(session_id) as binding:
                assert binding is not None
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT current_setting('app.current_tenant', true), "
                        "current_setting('app.current_user_id', true)"
                    )
                    row = cursor.fetchone()
                assert row is not None
                assert row[0] in (None, "")
                assert row[1] in (None, "")
                # Ordinary tenant reads stay denied inside the patient context.
                assert Patient.objects.count() == 0
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_setting('app.current_tenant', true), "
                    "current_setting('app.current_user_id', true)"
                )
                row = cursor.fetchone()
            assert row is not None
            assert row[0] in (None, "")
            assert row[1] in (None, "")
            assert Patient.objects.count() == 0
    finally:
        clear_connection_tenant_gucs()


def test_session_idle_and_absolute_expiry_fail_closed(
    rbac_graph: RbacGraph,
) -> None:
    access = _seed_patients(rbac_graph)
    _, secret = _issue(rbac_graph, access.enrollment_id)
    session_id = _redeem(rbac_graph.clinic_a, secret)
    assert session_id is not None

    # Idle past 30 minutes: the touch refuses and does not renew.
    _age_session(session_id, rbac_graph.organization_a, idle=timedelta(minutes=31))
    with runtime_role(), patient_session_context(session_id) as binding:
        assert binding is None
    assert (
        _session(session_id, rbac_graph.organization_a).idle_expires_at < timezone.now()
    )

    # Absolute expiry past 8 hours refuses even with a fresh idle window.
    _, second_secret = _issue(rbac_graph, access.enrollment_id)
    second_id = _redeem(rbac_graph.clinic_a, second_secret)
    assert second_id is not None
    _age_session(
        second_id,
        rbac_graph.organization_a,
        absolute=timedelta(minutes=1),
    )
    with runtime_role(), patient_session_context(second_id) as binding:
        assert binding is None


def test_staff_revocation_ends_the_invitation_and_its_sessions(
    rbac_graph: RbacGraph,
) -> None:
    access = _seed_patients(rbac_graph)
    grant_id, secret = _issue(rbac_graph, access.enrollment_id)
    session_id = _redeem(rbac_graph.clinic_a, secret)
    assert session_id is not None

    with service_runtime_role(), _as_manager(rbac_graph):
        revoke_patient_access(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=access.enrollment_id,
            grant_id=grant_id,
        )

    grant = _grant(grant_id, rbac_graph.organization_a)
    assert grant.revoked_at is not None
    assert _session(session_id, rbac_graph.organization_a).revoked_at is not None
    with runtime_role(), patient_session_context(session_id) as binding:
        assert binding is None

    # Revocation is idempotent and bound to the enrollment.
    with service_runtime_role(), _as_manager(rbac_graph):
        revoke_patient_access(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=access.enrollment_id,
            grant_id=grant_id,
        )
        with pytest.raises(PatientAccessDeniedError):
            revoke_patient_access(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=access.other_enrollment_id,
                grant_id=grant_id,
            )
        with pytest.raises(PatientAccessDeniedError):
            revoke_patient_access(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=access.enrollment_id,
                grant_id=uuid4(),
            )


def test_patient_overview_requires_a_live_bound_session(
    rbac_graph: RbacGraph,
) -> None:
    access = _seed_patients(rbac_graph)
    _, secret = _issue(rbac_graph, access.enrollment_id)
    session_id = _redeem(rbac_graph.clinic_a, secret)
    assert session_id is not None

    # Without the session GUC the resolver returns nothing.
    with runtime_role(), transaction.atomic():
        assert patient_session_overview() is None

    with runtime_role(), patient_session_context(session_id) as binding:
        assert binding is not None
        overview = patient_session_overview()
    assert overview is not None
    assert overview.patient_name == "Ana Synthetic Access"
    assert overview.clinic_name == "Todo 8 Clinic A"
    assert overview.operations == tuple(PATIENT_OPERATION_VALUES)

    # A revoked session resolves to nothing.
    with runtime_role():
        end_patient_session(session_id)
    with runtime_role(), patient_session_context(session_id) as binding:
        assert binding is None


def test_access_services_deny_foreign_and_unassigned_clinics(
    rbac_graph: RbacGraph,
) -> None:
    access = _seed_patients(rbac_graph)

    denied = (
        (rbac_graph.physician, rbac_graph.clinic_a),
        (rbac_graph.shared_user, rbac_graph.clinic_b),
        (rbac_graph.shared_user, rbac_graph.clinic_c),
    )
    for actor_id, clinic_id in denied:
        with (
            service_runtime_role(),
            tenant_context(actor_id, rbac_graph.organization_a),
        ):
            with pytest.raises(PatientAccessDeniedError):
                issue_invitation(
                    clinic_id=clinic_id,
                    enrollment_id=access.enrollment_id,
                )
            with pytest.raises(PatientAccessDeniedError):
                access_overview(
                    clinic_id=clinic_id,
                    enrollment_id=access.enrollment_id,
                )
            with pytest.raises(PatientAccessDeniedError):
                revoke_patient_access(
                    clinic_id=clinic_id,
                    enrollment_id=access.enrollment_id,
                    grant_id=uuid4(),
                )


def test_http_issue_redeem_view_and_logout_flow(
    rbac_graph: RbacGraph,
) -> None:
    access = _seed_patients(rbac_graph)
    receptionist = create_receptionist(rbac_graph)
    staff = Client()
    with runtime_role():
        assert staff.login(
            username=receptionist.username,
            password=OTP_RAW_CREDENTIAL,
        )
    access_url = f"/intake/clinics/{rbac_graph.clinic_a}/access/"

    with runtime_role():
        blank = staff.get(access_url)
        assert blank.status_code == 200
        assert b"Ana Synthetic Access" not in blank.content

        manage = staff.post(
            access_url,
            {"action": "manage", "enrollment_id": str(access.enrollment_id)},
        )
        assert manage.status_code == 200
        assert b"Ana Synthetic Access" in manage.content
        assert gettext("No invitations issued yet.").encode() in manage.content

        issued = staff.post(
            access_url,
            {"action": "issue", "enrollment_id": str(access.enrollment_id)},
        )
        assert issued.status_code == 200
        content = issued.content.decode()
        assert gettext("Invitation code") in content
        assert issued.wsgi_request is not None
        assert "code=" not in issued.wsgi_request.META.get("QUERY_STRING", "")

    # Recover the code from the rendered one-time panel.
    marker = 'id="issued-code">'
    secret = content.split(marker, 1)[1].split("<", 1)[0]
    grants = _grants(
        rbac_graph.organization_a,
        enrollment_id=access.enrollment_id,
    )
    assert len(grants) == 1
    assert hashlib.sha256(secret.encode()).digest() == bytes(grants[0].secret_hash)

    # The patient redeems the code through the POST body only.
    patient = Client()
    redeem_url = f"/patient/access/{rbac_graph.clinic_a}/"
    with runtime_role():
        form_page = patient.get(redeem_url)
        assert form_page.status_code == 200
        redeemed = patient.post(redeem_url, {"code": secret})
        assert redeemed.status_code == 302
        location = redeemed.headers.get("Location", "")
        assert location == "/patient/"
        assert secret not in location

        home = patient.get("/patient/")
        assert home.status_code == 200
        assert b"Ana Synthetic Access" in home.content
        assert b"Todo 8 Clinic A" in home.content

        # Patient sessions cannot reach staff surfaces.
        assert patient.get(access_url).status_code == 403
        assert patient.get("/auth/protected/").status_code == 403

        # Logout revokes the session server-side.
        signed_out = patient.post("/patient/logout/")
        assert signed_out.status_code == 200
        assert gettext("You are signed out").encode() in signed_out.content
        assert patient.get("/patient/").status_code == 403

    sessions = _sessions(
        rbac_graph.organization_a,
        grant_id=grants[0].pk,
    )
    assert len(sessions) == 1
    assert sessions[0].revoked_at is not None


def test_http_redeem_failures_are_identical_and_non_enumerating(
    rbac_graph: RbacGraph,
) -> None:
    access = _seed_patients(rbac_graph)
    grant_id, secret = _issue(rbac_graph, access.enrollment_id)
    patient = Client()
    redeem_a = f"/patient/access/{rbac_graph.clinic_a}/"
    redeem_b = f"/patient/access/{rbac_graph.clinic_b}/"

    with runtime_role():
        unknown = patient.post(redeem_a, {"code": "bogus-code"})
        assert unknown.status_code == 200
        assert INVALID_CODE_MESSAGE in unknown.content.decode()

        # Wrong clinic: identical status and message.
        wrong_clinic = patient.post(redeem_b, {"code": secret})
        assert wrong_clinic.status_code == 200
        assert INVALID_CODE_MESSAGE in wrong_clinic.content.decode()

        # Consumed: identical failure.
        assert patient.post(redeem_a, {"code": secret}).status_code == 302
        consumed = patient.post(redeem_a, {"code": secret})
        assert consumed.status_code == 200
        assert INVALID_CODE_MESSAGE in consumed.content.decode()

    # Expired: identical failure.
    _expire_grant(grant_id, rbac_graph.organization_a)
    second_grant_id, second_secret = _issue(rbac_graph, access.enrollment_id)
    _expire_grant(second_grant_id, rbac_graph.organization_a)
    with runtime_role():
        expired = patient.post(redeem_a, {"code": second_secret})
        assert expired.status_code == 200
        body = expired.content.decode()
        assert INVALID_CODE_MESSAGE in body
        # The failure never names the reason.
        for distinguishing in ("expired", "consumed", "used", "revoked"):
            assert distinguishing not in body.lower()


def test_http_patient_routes_fail_closed_without_a_session(
    rbac_graph: RbacGraph,
) -> None:
    del rbac_graph
    client = Client()
    with runtime_role():
        assert client.get("/patient/").status_code == 403
        assert client.get("/patient/anything/").status_code == 403
        # A forged session id in the signed cookie is rejected the same way.
        forged = _patient_client(uuid4())
        assert forged.get("/patient/").status_code == 403
        # The redemption GET is the only patient path open without a session.
        assert client.get(f"/patient/access/{uuid4()}/").status_code == 200


def test_http_forged_context_and_swapped_ids_change_nothing(
    rbac_graph: RbacGraph,
) -> None:
    access = _seed_patients(rbac_graph)
    _, secret = _issue(rbac_graph, access.enrollment_id)
    patient = Client()
    redeem_url = f"/patient/access/{rbac_graph.clinic_a}/"

    with runtime_role():
        # Forged organization/patient fields in the body are ignored.
        forged = patient.post(
            redeem_url,
            {
                "code": secret,
                "organization_id": str(rbac_graph.organization_b),
                "patient_id": str(access.other_patient_id),
                "enrollment_id": str(access.other_enrollment_id),
            },
        )
        assert forged.status_code == 302
        home = patient.get("/patient/")
        assert home.status_code == 200
        # The binding is the grant's enrollment, never the posted ids.
        assert b"Todo 8 Clinic A" in home.content
        assert b"Todo 8 Clinic B" not in home.content
        assert b"Bia Synthetic Access" not in home.content

        # A patient session cannot mint another session or reach staff data.
        assert patient.post(redeem_url, {"code": secret}).status_code == 200
        staff_only = patient.post(
            f"/intake/clinics/{rbac_graph.clinic_a}/access/",
            {"action": "issue", "enrollment_id": str(access.enrollment_id)},
        )
        assert staff_only.status_code == 403

    sessions = _sessions(rbac_graph.organization_a)
    assert len(sessions) == 1
    session = sessions[0]
    assert session.patient_id == access.patient_id
    assert session.enrollment_id == access.enrollment_id
    assert session.organization_id == rbac_graph.organization_a


def test_http_revoked_session_denies_and_staff_session_is_unchanged(
    rbac_graph: RbacGraph,
) -> None:
    access = _seed_patients(rbac_graph)
    receptionist = create_receptionist(rbac_graph)
    grant_id, secret = _issue(rbac_graph, access.enrollment_id)
    patient = Client()

    with runtime_role():
        # Redeem through the real client so the cookie carries the session.
        redeem_url = f"/patient/access/{rbac_graph.clinic_a}/"
        assert patient.post(redeem_url, {"code": secret}).status_code == 302
        assert patient.get("/patient/").status_code == 200

    with service_runtime_role(), _as_manager(rbac_graph):
        revoke_patient_access(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=access.enrollment_id,
            grant_id=grant_id,
        )

    with runtime_role():
        denied = patient.get("/patient/")
        assert denied.status_code == 403
        # The denial is the generic gate, not an enumeration of the session.
        assert gettext("Access required").encode() in denied.content

        # Ordinary staff sessions are unaffected by patient machinery.
        staff = Client()
        assert staff.login(
            username=receptionist.username,
            password=OTP_RAW_CREDENTIAL,
        )
        assert staff.get("/auth/protected/").status_code == 200
        staff_list = staff.get(f"/intake/clinics/{rbac_graph.clinic_a}/patients/")
        assert staff_list.status_code == 200
