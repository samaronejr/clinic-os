"""Real signed-cookie/session inventory and immediate revocation regressions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, assert_never

import pytest
from apps.identity.models import SessionRecord, User, UserClinicRole
from apps.identity.session_inventory import (
    SessionInventoryDeniedError,
    own_sessions,
    revoke_session,
    session_hash,
)
from apps.tenancy.db import tenant_context
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.db import DatabaseError, connection, transaction
from django.test import Client
from django.utils import timezone

from identity.lifecycle_test_support import session_key, verified_client
from identity.permission_support import owner_context
from otp_test_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_own_revoke_rejects_reused_cookie_on_next_request(
    rbac_graph: RbacGraph,
) -> None:
    # Given a real verified login and its current inventory record.
    client = verified_client(rbac_graph)
    key = session_key(client)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        record = next(
            row
            for row in own_sessions()
            if bytes(row.session_key_hash) == session_hash(key)
        )
        # When this user revokes that inventory record.
        revoke_session(session_record_id=record.pk)
    with runtime_role():
        response = client.get("/auth/protected/")
    # Then the old authenticated cookie cannot open another request.
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/auth/login/")


def test_foreign_session_revocation_preserves_other_user(
    rbac_graph: RbacGraph,
) -> None:
    other = Client()
    other_user = User.objects.get(pk=rbac_graph.shared_user)
    with runtime_role():
        other.force_login(other_user)
    with owner_context(rbac_graph.organization_a):
        foreign = SessionRecord.objects.get(
            session_key_hash=session_hash(session_key(other))
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        pytest.raises(SessionInventoryDeniedError),
    ):
        revoke_session(session_record_id=foreign.pk)
    with owner_context(rbac_graph.organization_a):
        assert SessionRecord.objects.get(pk=foreign.pk).revoked_at is None


@pytest.mark.parametrize("deleted_payload", [False, True])
@pytest.mark.parametrize("htmx", [False, True])
def test_revoked_ui_api_cookie_returns_json_without_session_mutation(
    rbac_graph: RbacGraph, *, deleted_payload: bool, htmx: bool
) -> None:
    # Given a revoked real session, with either its payload or only its tombstone.
    client = verified_client(rbac_graph)
    key = session_key(client)
    with owner_context(rbac_graph.organization_a):
        SessionRecord.objects.filter(session_key_hash=session_hash(key)).update(
            revoked_at=timezone.now()
        )
        if deleted_payload:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM clinic_app.django_session WHERE session_key = %s",
                    [key],
                )
    # When the browser sends that cookie to the UI API.
    with runtime_role():
        response = client.post(
            "/api/ui/v1/agenda/query/",
            {},
            content_type="application/json",
            headers={"HX-Request": "true"} if htmx else {},
        )
    # Then the API remains a side-effect-free JSON refusal, never a login page.
    assert response.status_code == 403
    assert response.json() == {
        "code": "access_denied",
        "message_key": "api.error.access_denied",
    }
    assert "Location" not in response.headers
    assert "HX-Redirect" not in response.headers
    assert settings.SESSION_COOKIE_NAME not in response.cookies
    assert client.cookies[settings.SESSION_COOKIE_NAME].value == key


@pytest.mark.parametrize("cookie", ["synthetic-unknown-session", "synthetic-\u00e9"])
def test_unknown_cookie_is_refused_without_mutating_the_cookie(cookie: str) -> None:
    client = Client()
    client.cookies[settings.SESSION_COOKIE_NAME] = cookie
    with runtime_role():
        response = client.get("/auth/protected/")
    assert response.status_code == 403
    assert settings.SESSION_COOKIE_NAME not in response.cookies


def test_runtime_cannot_forge_a_rotation_marker(rbac_graph: RbacGraph) -> None:
    client = verified_client(rbac_graph)
    digest = session_hash(session_key(client))
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        with pytest.raises(DatabaseError) as failure, transaction.atomic():
            SessionRecord.objects.filter(session_key_hash=digest).update(
                superseded_at=timezone.now(),
                revoked_at=timezone.now(),
            )
        assert getattr(failure.value.__cause__, "sqlstate", None) == "23514"
        record = SessionRecord.objects.get(session_key_hash=digest)
        assert record.superseded_at is None
        assert record.revoked_at is None


def test_revoke_hash_callable_revokes_only_the_bound_account(
    rbac_graph: RbacGraph,
) -> None:
    # Given another account's live session digest and this caller's actor
    # context, the resolver-owned callable cannot be aimed at a foreign row.
    victim_user = User.objects.get(pk=rbac_graph.shared_user)
    victim = Client()
    with runtime_role():
        victim.force_login(victim_user)
    foreign_digest = session_hash(session_key(victim))
    with owner_context(rbac_graph.organization_a):
        foreign = SessionRecord.objects.get(session_key_hash=foreign_digest)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT clinic_app.identity_session_revoke_hash(%s)",
            [foreign_digest],
        )
        assert cursor.fetchone() == (False,)
    with owner_context(rbac_graph.organization_a):
        assert SessionRecord.objects.get(pk=foreign.pk).revoked_at is None


def test_logout_tombstones_the_authenticated_session(
    rbac_graph: RbacGraph,
) -> None:
    # A CSRF-protected logout keeps writing the tombstone the stale-cookie
    # redirect branch depends on, under the bound authenticated scope.
    client = verified_client(rbac_graph)
    digest = session_hash(session_key(client))
    with runtime_role():
        response = client.post("/auth/logout/")
    assert response.status_code == 302
    with owner_context(rbac_graph.organization_a):
        record = SessionRecord.objects.get(session_key_hash=digest)
        assert record.revoked_at is not None


@pytest.mark.parametrize("change", ["password", "role"])
def test_password_or_role_change_revokes_all_sessions(
    rbac_graph: RbacGraph, change: Literal["password", "role"]
) -> None:
    first = verified_client(rbac_graph)
    second = Client()
    user = User.objects.get(pk=rbac_graph.clinic_admin)
    with runtime_role():
        second.force_login(user)
    keys = (session_hash(session_key(first)), session_hash(session_key(second)))
    # When an owner changes the actual password or retires the actual role.
    with owner_context(rbac_graph.organization_a):
        match change:
            case "password":
                user.password = make_password("synthetic-new-password-for-revocation")
                user.save(update_fields=("password",))
            case "role":
                membership = UserClinicRole.objects.get(
                    user=user, clinic_id=rbac_graph.clinic_b
                )
                membership.revoked_at = timezone.now()
                membership.save(update_fields=("revoked_at",))
            case unreachable:
                assert_never(unreachable)
        # Then every inventory key is immediately a tombstone.
        assert not SessionRecord.objects.filter(
            session_key_hash__in=keys, revoked_at__isnull=True
        ).exists()
