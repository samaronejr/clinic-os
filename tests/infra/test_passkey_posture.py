"""Passkey isolation and metadata write boundaries on the actual database roles."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.identity.models import Passkey, SessionRecord
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.models.functions import Now

from auth.passkey_test_support import registered_passkey
from identity.lifecycle_test_support import session_key
from identity.permission_support import owner_context
from otp_test_support import runtime_role

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _webauthn_settings(settings: SettingsWrapper) -> None:
    settings.WEBAUTHN_RP_ID = "testserver"
    settings.WEBAUTHN_ORIGIN = "http://testserver"


def test_credentials_are_force_rls_and_only_own_current_org_rows_are_visible(
    rbac_graph: RbacGraph,
) -> None:
    registered_passkey(rbac_graph)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity "
            "FROM pg_class WHERE oid = 'clinic_app.identity_passkey'::regclass"
        )
        assert cursor.fetchone() == (True, True)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        assert Passkey.objects.count() == 1
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        assert not Passkey.objects.exists()
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_b),
    ):
        assert not Passkey.objects.exists()
    with runtime_role():
        assert not Passkey.objects.exists()


@pytest.mark.parametrize(
    "column", ["public_key", "credential_id", "user_id", "sign_count", "last_used"]
)
def test_runtime_cannot_write_public_identity_counter_or_use_metadata(
    rbac_graph: RbacGraph, column: str
) -> None:
    registered_passkey(rbac_graph)
    updates = {
        "public_key": "UPDATE clinic_app.identity_passkey SET public_key = public_key",
        "credential_id": (
            "UPDATE clinic_app.identity_passkey SET credential_id = credential_id"
        ),
        "user_id": "UPDATE clinic_app.identity_passkey SET user_id = user_id",
        "sign_count": "UPDATE clinic_app.identity_passkey SET sign_count = sign_count",
        "last_used": "UPDATE clinic_app.identity_passkey SET last_used = last_used",
    }
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        pytest.raises(DatabaseError),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        # Even a no-op raw update must lack the column grant; callers cannot forge use.
        cursor.execute(updates[column])


def test_runtime_cannot_forge_a_persisted_passkey_verification(
    rbac_graph: RbacGraph,
) -> None:
    registered = registered_passkey(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        passkey = Passkey.objects.get(user_id=rbac_graph.clinic_admin)
        assert SessionRecord.objects.filter(user_id=rbac_graph.clinic_admin).exists()
        with pytest.raises(IntegrityError), transaction.atomic():
            SessionRecord.objects.filter(user_id=rbac_graph.clinic_admin).update(
                verification_passkey_id=passkey.pk
            )
    assert session_key(registered.client)


def test_owner_cannot_hard_delete_truncate_or_resurrect_a_removed_credential(
    rbac_graph: RbacGraph,
) -> None:
    registered_passkey(rbac_graph)
    with owner_context(rbac_graph.organization_a):
        passkey = Passkey.objects.get(user_id=rbac_graph.clinic_admin)
        with pytest.raises(IntegrityError), transaction.atomic():
            Passkey.objects.filter(pk=passkey.pk).delete()
        with (
            pytest.raises(IntegrityError),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute("TRUNCATE clinic_app.identity_passkey CASCADE")
        Passkey.objects.filter(pk=passkey.pk).update(removed_at=Now())
        with pytest.raises(IntegrityError), transaction.atomic():
            Passkey.objects.filter(pk=passkey.pk).update(removed_at=None)
