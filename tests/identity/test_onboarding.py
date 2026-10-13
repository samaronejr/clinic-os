"""Real PostgreSQL and HTTP proof of the original15 onboarding boundary."""

from __future__ import annotations

import secrets
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.services import verify_chain
from apps.identity.models import OnboardingProgress, Organization, UserClinicRole
from apps.identity.onboarding import onboarding_progress
from apps.identity.signup import (
    SignupCapture,
    SignupDeniedError,
    inspect_pending_signup,
    pending_signups,
    submit_signup,
)
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction
from django.test import Client

from identity.permission_support import owner_context

if TYPE_CHECKING:
    from collections.abc import Generator

    from rbac_fixtures import RbacGraph

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("synthetic_secret_backend"),
]


@contextmanager
def _runtime_role() -> Generator[None, None, None]:
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE clinic_app")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")


@contextmanager
def _owner_role() -> Generator[None, None, None]:
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE clinic_owner")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")


def _capture() -> SignupCapture:
    return SignupCapture(
        organization_name="Sintetico Organizacao de Abertura",
        cnpj="11.222.333/0001-81",
        legal_responsible="Sintetico Responsavel Legal",
        username=f"sintetico-{uuid4().hex}",
        email=f"sintetico-{uuid4().hex}@example.invalid",
        password=secrets.token_urlsafe(32),
        professional_registration="Sintetico CRM SP 12345",
    )


def test_signup_is_protected_pending_and_confers_no_runtime_membership() -> None:
    request_id = uuid4()
    client = Client()
    raw_credential = secrets.token_urlsafe(32)
    with _runtime_role():
        response = client.post(
            "/auth/signup/",
            {
                "organization_name": "Sintetico Organizacao de Abertura",
                "cnpj": "11.222.333/0001-81",
                "legal_responsible": "Sintetico Responsavel Legal",
                "username": f"sintetico-{uuid4().hex}",
                "email": f"sintetico-{uuid4().hex}@example.invalid",
                "password": raw_credential,
                "password_confirm": raw_credential,
                "professional_registration": "Sintetico CRM SP 12345",
                "idempotency_key": str(request_id),
            },
        )
        assert response.status_code == 200
        assert b'data-signup-state="pending_activation"' in response.content
        assert "_auth_user_id" not in client.session
        assert not Organization.objects.exists()
        assert not UserClinicRole.objects.exists()

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT organization_id, first_admin_id, payload
            FROM clinic_app.identity_signupregistration WHERE idempotency_key = %s
            """,
            [request_id],
        )
        registration = cursor.fetchone()
        assert registration is not None
        organization_id, admin_id, ciphertext = registration
        assert bytes(ciphertext).startswith(b"\x01")
        assert b"Sintetico Responsavel Legal" not in bytes(ciphertext)
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        cursor.execute(
            """
            SELECT status, name, cnpj FROM clinic_app.identity_organization
            WHERE id = %s
            """,
            [organization_id],
        )
        assert cursor.fetchone() == ("pending_activation", "", "")
        cursor.execute(
            "SELECT is_active, email FROM clinic_app.identity_user WHERE id = %s",
            [admin_id],
        )
        assert cursor.fetchone() == (False, "")
        cursor.execute(
            """
            SELECT count(*) FROM clinic_app.identity_userclinicrole WHERE user_id = %s
            """,
            [admin_id],
        )
        assert cursor.fetchone() == (0,)


def test_signup_replay_is_idempotent_and_changed_capture_is_refused() -> None:
    capture, request_id = _capture(), uuid4()
    with _runtime_role():
        first = submit_signup(capture=capture, idempotency_key=request_id)
        replay = submit_signup(capture=capture, idempotency_key=request_id)
        assert first.created is True
        assert replay.created is False
        assert replay.organization_id == first.organization_id
        assert replay.first_admin_id == first.first_admin_id
        with pytest.raises(SignupDeniedError):
            submit_signup(
                capture=replace(
                    capture, legal_responsible="Sintetico Outro Responsavel"
                ),
                idempotency_key=request_id,
            )
    with _owner_role():
        assert len(pending_signups()) == 1
        verification = verify_chain()
        assert verification.valid is True
        assert verification.row_count == 1


def test_pending_capture_is_owner_only_and_audit_hash_is_valid() -> None:
    capture, request_id = _capture(), uuid4()
    with _runtime_role():
        receipt = submit_signup(capture=capture, idempotency_key=request_id)
        with pytest.raises(SignupDeniedError):
            pending_signups()
        with pytest.raises(SignupDeniedError):
            inspect_pending_signup(signup_id=uuid4())
    with _owner_role():
        pending = pending_signups()
        assert len(pending) == 1
        protected = inspect_pending_signup(signup_id=pending[0].pk)
        assert protected["cnpj"] == "11222333000181"
        assert protected["legal_responsible"] == capture.legal_responsible
        assert (
            protected["professional_registration"] == capture.professional_registration
        )
        assert protected["username"] == capture.username
        assert "password" not in protected
        assert pending[0].organization_id == receipt.organization_id
        verification = verify_chain()
        assert verification.valid is True
        assert verification.row_count == 2


def test_pending_organization_is_invisible_even_with_forged_tenant_guc() -> None:
    with _runtime_role():
        receipt = submit_signup(capture=_capture(), idempotency_key=uuid4())
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant', %s, true)",
                [str(receipt.organization_id)],
            )
            assert not Organization.objects.exists()
        with (
            pytest.raises(DatabaseError),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute("SELECT * FROM clinic_app.identity_signupregistration")


def test_signup_capture_has_exact_forced_owner_only_posture() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT relrowsecurity, relforcerowsecurity, relowner::regrole::text
            FROM pg_class WHERE oid = 'clinic_app.identity_signupregistration'::regclass
            """
        )
        assert cursor.fetchone() == (True, True, "clinic_owner")
        cursor.execute(
            """
            SELECT policyname, roles, cmd, qual, with_check FROM pg_policies
            WHERE schemaname = 'clinic_app'
              AND tablename = 'identity_signupregistration'
            """
        )
        assert cursor.fetchall() == [
            ("signup_owner", ["clinic_owner"], "ALL", "true", "true")
        ]
        cursor.execute(
            """
            SELECT grantee, privilege_type FROM information_schema.role_table_grants
            WHERE table_schema = 'clinic_app'
              AND table_name = 'identity_signupregistration'
              AND grantee IN ('clinic_app', 'clinic_agent', 'clinic_resolver', 'PUBLIC')
            """
        )
        assert cursor.fetchall() == []


@pytest.mark.parametrize(
    "guc",
    [
        "app.current_user_id",
        "app.current_principal",
        "app.current_patient_session",
        "app.current_tenant",
    ],
)
def test_signup_refuses_ambient_identity_or_tenant_authority(guc: str) -> None:
    with _runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT set_config(%s, %s, true)", [guc, str(uuid4())])
        with pytest.raises(SignupDeniedError):
            submit_signup(capture=_capture(), idempotency_key=uuid4())
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM clinic_app.identity_signupregistration")
        assert cursor.fetchone() == (0,)


def test_core_onboarding_steps_are_resumable(rbac_graph: RbacGraph) -> None:
    with (
        _runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        first = onboarding_progress(clinic_id=rbac_graph.clinic_b, persist=True)
        second = onboarding_progress(clinic_id=rbac_graph.clinic_b, persist=True)

    assert {step.key for step in first} == {
        "organization_unit",
        "staff_invitations",
        "recovery_enrollment",
        "provider_connections",
        "opening_agenda",
    }
    assert second == first
    with owner_context(rbac_graph.organization_a):
        assert (
            OnboardingProgress.objects.filter(clinic_id=rbac_graph.clinic_b).count()
            == 5
        )
