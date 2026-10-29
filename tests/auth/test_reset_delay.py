"""Execute the real database reset boundary with only its clock controlled."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from apps.identity.lifecycle_authority import LifecycleDeniedError
from apps.identity.models import AuthenticatorRetirement, SessionRecord
from apps.identity.recovery import execute_authenticator_reset
from apps.identity.session_inventory import SessionInventoryDeniedError
from apps.tenancy.db import tenant_context
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from auth.test_recovery_codes import _deliver, _requested_reset
from identity.lifecycle_test_support import session_key, verified_client
from identity.permission_support import owner_context
from otp_test_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from datetime import datetime

    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def reset_db_clock() -> Iterator[Callable[[datetime], None]]:
    """Retain exact installed guards; replace only their database clock calls."""
    signatures = (
        "clinic_app.identity_reset_notification_clock()",
        "clinic_app.identity_reset_execute(uuid,bytea)",
    )
    definitions: list[str] = []
    with connection.cursor() as cursor:
        for signature in signatures:
            cursor.execute("SELECT pg_get_functiondef(%s::regprocedure)", [signature])
            row = cursor.fetchone()
            assert row is not None
            definitions.append(row[0])
        cursor.execute(
            "CREATE FUNCTION clinic_app.identity_test_reset_clock() "
            "RETURNS timestamptz LANGUAGE sql STABLE "
            "AS 'SELECT current_setting(''app.test_reset_clock'')::timestamptz'"
        )
        cursor.execute(
            "REVOKE ALL ON FUNCTION clinic_app.identity_test_reset_clock() FROM PUBLIC"
        )
        cursor.execute(
            "GRANT EXECUTE ON FUNCTION clinic_app.identity_test_reset_clock() "
            "TO clinic_app, clinic_resolver, clinic_owner"
        )
        cursor.execute("SET ROLE clinic_resolver")
        try:
            for definition in definitions:
                assert "statement_timestamp()" in definition
                cursor.execute(
                    definition.replace(
                        "statement_timestamp()",
                        "clinic_app.identity_test_reset_clock()",
                    )
                )
        finally:
            cursor.execute("RESET ROLE")

    def set_clock(value: datetime) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.test_reset_clock', %s, false)",
                [value.isoformat()],
            )

    try:
        yield set_clock
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE clinic_resolver")
            try:
                for definition in definitions:
                    cursor.execute(definition)
            finally:
                cursor.execute("RESET ROLE")
            cursor.execute("DROP FUNCTION clinic_app.identity_test_reset_clock()")
            cursor.execute("RESET app.test_reset_clock")


def test_authenticated_delivery_and_exact_database_deadline_retire_all(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    reset_db_clock: Callable[[datetime], None],
) -> None:
    settings.COMMS_SYNTHETIC_CHANNELS = ("email",)
    client = verified_client(rbac_graph)
    reset = _requested_reset(rbac_graph, client)
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE clinic_app.identity_user SET email=%s WHERE id=%s",
            ["synthetic-clock@example.invalid", rbac_graph.clinic_admin],
        )
    reset_db_clock(timezone.now())
    _deliver(reset)
    with owner_context(rbac_graph.organization_a):
        reset.refresh_from_db()
    assert reset.notified_at is not None
    assert reset.available_at == reset.notified_at + timedelta(hours=24)
    assert reset.available_at is not None
    reset_db_clock(reset.available_at - timedelta(microseconds=1))
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        pytest.raises(LifecycleDeniedError),
    ):
        execute_authenticator_reset(
            clinic_id=rbac_graph.clinic_b,
            reset_id=reset.pk,
            session_key=session_key(client),
        )
    reset_db_clock(reset.available_at)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        execute_authenticator_reset(
            clinic_id=rbac_graph.clinic_b,
            reset_id=reset.pk,
            session_key=session_key(client),
        )
        with pytest.raises(SessionInventoryDeniedError):
            execute_authenticator_reset(
                clinic_id=rbac_graph.clinic_b,
                reset_id=reset.pk,
                session_key=session_key(client),
            )
    with owner_context(rbac_graph.organization_a):
        reset.refresh_from_db()
        assert reset.executed_at == reset.available_at
        assert AuthenticatorRetirement.objects.filter(
            user_id=rbac_graph.clinic_admin
        ).exists()
        assert not SessionRecord.objects.filter(
            user_id=rbac_graph.clinic_admin, revoked_at__isnull=True
        ).exists()


def test_native_reset_form_refuses_early_and_executes_at_database_deadline(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    reset_db_clock: Callable[[datetime], None],
) -> None:
    graph = rbac_graph
    settings.COMMS_SYNTHETIC_CHANNELS = ("email",)
    client = verified_client(graph)
    reset = _requested_reset(graph, client)
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE clinic_app.identity_user SET email=%s WHERE id=%s",
            ["synthetic-native-clock@example.invalid", graph.clinic_admin],
        )
    reset_db_clock(timezone.now())
    _deliver(reset)
    with owner_context(graph.organization_a):
        reset.refresh_from_db()
    assert reset.available_at is not None
    path = reverse("identity:reset-execute", kwargs={"clinic_id": graph.clinic_b})
    reset_db_clock(reset.available_at - timedelta(microseconds=1))
    with runtime_role():
        early = client.post(path, {"reset_id": str(reset.pk)})
    assert early.status_code == 409
    reset_db_clock(reset.available_at)
    with runtime_role():
        completed = client.post(path, {"reset_id": str(reset.pk)})
    assert completed.status_code == 302
    assert completed.headers["Location"] == reverse(
        "identity:staff", kwargs={"clinic_id": graph.clinic_b}
    )
    with owner_context(graph.organization_a):
        reset.refresh_from_db()
        assert reset.executed_at == reset.available_at
        assert AuthenticatorRetirement.objects.filter(
            user_id=graph.clinic_admin
        ).exists()
