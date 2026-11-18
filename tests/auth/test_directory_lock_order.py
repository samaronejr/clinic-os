"""SCIM deprovisioning and concurrent retirement keep one lock order (S3).

Same interleaving as ``test_session_lock_order.py``: connection H holds the
first lock of a real path, path P blocks on it, a monitor waits server-side
until PostgreSQL reports P blocked, and H runs its real second step. In the
fixed order P is still waiting on a row and holds nothing H needs, so both
complete. Before identity 0057 P already held what H's second step needs, and
PostgreSQL aborted one of them with ``deadlock_detected``.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.identity.enterprise import approve_group_mapping
from apps.identity.models import User, UserClinicRole
from apps.identity.scim import create_resource, scim_context
from django.db import connection, connections
from django.test import Client, override_settings

from auth.passkey_test_support import registered_passkey
from auth.test_session_lock_order import SECOND_STEP_TIMEOUT, _await_blocked, _outcome
from identity.enterprise_support import enterprise_setup
from identity.permission_support import owner_context, permission_context
from otp_test_support import create_totp_device
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _directory_receptionist(graph: RbacGraph) -> tuple[str, UUID, str]:
    """A directory user whose group grants a directory-managed membership."""
    setup = enterprise_setup(graph)
    with runtime_role(), scim_context(bearer=setup.token) as scope:
        user = create_resource(
            clinic_id=scope.clinic_id,
            kind="Users",
            attributes={"userName": f"synthetic-{uuid4().hex}", "active": True},
        )
        group = create_resource(
            clinic_id=scope.clinic_id,
            kind="Groups",
            attributes={
                "displayName": "Synthetic reception",
                "members": [{"value": str(user.pk)}],
            },
        )
    with permission_context(graph, graph.clinic_admin):
        approve_group_mapping(
            clinic_id=graph.clinic_a,
            group_id=group.pk,
            unit_id=graph.clinic_a,
            bundle="receptionist",
            session_key=setup.session_key,
        )
    assert user.user_id is not None
    return str(user.pk), user.user_id, setup.token


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_scim_deprovisioning_and_password_change_cannot_deadlock(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    # Deprovisioning revokes the membership (its trigger takes the session key)
    # and disables the user in one sync. A password change locks the user row,
    # then takes the session key in its trigger.
    resource, account, token = _directory_receptionist(rbac_graph)
    with owner_context(rbac_graph.organization_a):
        assert UserClinicRole.objects.filter(
            user_id=account, revoked_at__isnull=True
        ).exists()
    deprovision: dict[str, object] = {}
    started = threading.Event()

    def deprovision_user() -> None:
        try:
            with runtime_role():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    row = cursor.fetchone()
                    assert row is not None
                    deprovision["pid"] = int(row[0])
                started.set()
                response = Client().patch(
                    f"/scim/v2/Users/{resource}",
                    data={
                        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                        "Operations": [
                            {"op": "replace", "path": "active", "value": False}
                        ],
                    },
                    content_type="application/scim+json",
                    HTTP_AUTHORIZATION=f"Bearer {token}",
                )
            deprovision["outcome"] = f"status {response.status_code}"
        except Exception as error:  # noqa: BLE001 - the outcome is the assertion
            deprovision["outcome"] = _outcome(error)
        finally:
            started.set()
            connections.close_all()

    with psycopg.connect(superuser_database_url) as password:
        password.execute(
            "SELECT 1 FROM clinic_app.identity_user WHERE id = %s FOR UPDATE",
            [account],
        )
        worker = threading.Thread(target=deprovision_user)
        worker.start()
        assert started.wait(timeout=30), "deprovisioning never started"
        assert "pid" in deprovision, deprovision
        _await_blocked(superuser_database_url, int(str(deprovision["pid"])))
        password.execute(SECOND_STEP_TIMEOUT)
        try:
            password.execute(
                "UPDATE clinic_app.identity_user SET password = password || 'x'"
                " WHERE id = %s",
                [account],
            )
            password.commit()
            changed = _outcome(None)
        except psycopg.Error as error:
            password.rollback()
            changed = _outcome(error)
    worker.join(timeout=60)
    assert not worker.is_alive()
    assert (changed, deprovision["outcome"]) == ("completed", "status 200")
    with owner_context(rbac_graph.organization_a):
        assert not User.objects.get(pk=account).is_active
        assert not UserClinicRole.objects.filter(
            user_id=account, revoked_at__isnull=True
        ).exists()


@override_settings(WEBAUTHN_RP_ID="testserver", WEBAUTHN_ORIGIN="http://testserver")
def test_two_retirements_of_one_user_cannot_deadlock(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    # Offboarding and recovery-code use lock the user's authenticator rows,
    # then retire; a concurrent retirement must not hold its retirement rows
    # while waiting for those authenticator rows.
    registered_passkey(rbac_graph)
    user: UUID = rbac_graph.clinic_admin
    create_totp_device(user, confirmed=True)
    retired: dict[str, str] = {}

    with (
        psycopg.connect(superuser_database_url) as offboarding,
        psycopg.connect(superuser_database_url) as retirement,
    ):
        offboarding.execute(
            "SELECT clinic_app.identity_lock_authenticators(%s)", [user]
        )

        def retire() -> None:
            try:
                retirement.execute(
                    "SELECT clinic_app.identity_retire_authenticators(%s)", [user]
                )
                retirement.commit()
                retired["outcome"] = _outcome(None)
            except psycopg.Error as error:
                retirement.rollback()
                retired["outcome"] = _outcome(error)

        worker = threading.Thread(target=retire)
        worker.start()
        _await_blocked(superuser_database_url, retirement.info.backend_pid)
        offboarding.execute(SECOND_STEP_TIMEOUT)
        try:
            offboarding.execute(
                "SELECT clinic_app.identity_retire_authenticators(%s)", [user]
            )
            offboarding.commit()
            offboarded = _outcome(None)
        except psycopg.Error as error:
            offboarding.rollback()
            offboarded = _outcome(error)
        worker.join(timeout=60)
        assert not worker.is_alive()
    assert (offboarded, retired["outcome"]) == ("completed", "completed")
