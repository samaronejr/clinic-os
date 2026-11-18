"""Row-lock modes and gate order on real deprovisioning paths (review S4).

A session-row INSERT's foreign keys take ``FOR KEY SHARE`` on the user and
passkey rows. Identity 0058 locks those rows ``FOR NO KEY UPDATE``, which does
not conflict with that, and has SCIM take the user gate before any row.

Each test follows ``test_session_lock_order.py``: H holds the first lock of a
real path, the path under test blocks on it, a monitor waits server-side
until PostgreSQL reports it blocked (bounded by ``statement_timeout``; no sleep),
and H runs its real second step under a 5 s ``lock_timeout``, longer than the
1 s deadlock detector. Before 0058 these interleavings deadlocked and
PostgreSQL aborted the SCIM run or the offboarding; now both sides complete.
"""

from __future__ import annotations

import hashlib
import threading
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.identity.models import User, UserClinicRole
from apps.identity.offboarding import offboard_staff
from apps.tenancy.db import tenant_context
from django.db import connection, connections
from django.test import Client, override_settings

from auth.test_directory_lock_order import _directory_receptionist
from auth.test_session_lock_order import SECOND_STEP_TIMEOUT, _await_blocked, _outcome
from identity.permission_support import owner_context
from identity.test_offboarding import _target
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

SESSION_KEY = (
    "SELECT pg_advisory_xact_lock(hashtextextended("
    "'clinic-lock-v1:sessions:' || %s::text, 0))"
)
USER_GATE = (
    "SELECT pg_advisory_xact_lock(hashtextextended("
    "'clinic-lock-v1:user:' || %s::text, 0))"
)
CURRENT_USER = "SELECT set_config('app.current_user_id', %s, true)"
Steps = list[tuple[str, list[object]]]
REGISTER = (
    "SELECT clinic_app.identity_session_register(%s, %s, '127.0.0.0/24',"
    " 'other', statement_timestamp() + interval '1 hour')"
)


class _Background:
    """Run one real path on its own Django connection; record pid and outcome."""

    def __init__(self, run: Callable[[], object]) -> None:
        self.result: dict[str, object] = {}
        self.started = threading.Event()
        self._run = run
        self.thread = threading.Thread(target=self._target)

    def _target(self) -> None:
        try:
            with runtime_role():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    row = cursor.fetchone()
                    assert row is not None
                    self.result["pid"] = int(row[0])
                self.started.set()
                self.result["outcome"] = self._run()
        except Exception as error:  # noqa: BLE001 - the outcome is the assertion
            self.result["outcome"] = _outcome(error)
        finally:
            self.started.set()
            connections.close_all()

    def start_and_await_blocked(self, url: str) -> None:
        self.thread.start()
        assert self.started.wait(timeout=30), "the path never started"
        assert "pid" in self.result, self.result
        _await_blocked(url, int(str(self.result["pid"])))

    def outcome(self) -> object:
        self.thread.join(timeout=60)
        assert not self.thread.is_alive()
        return self.result["outcome"]


def _deprovision(resource: str, token: str) -> _Background:
    def run() -> str:
        response = Client().patch(
            f"/scim/v2/Users/{resource}",
            data={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "replace", "path": "active", "value": False}],
            },
            content_type="application/scim+json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        return f"status {response.status_code}"

    return _Background(run)


def _second_step(holder: psycopg.Connection, statements: Steps) -> str:
    holder.execute(SECOND_STEP_TIMEOUT)
    try:
        for statement, parameters in statements:
            holder.execute(statement, parameters)
        holder.commit()
    except psycopg.Error as error:
        holder.rollback()
        return _outcome(error)
    return _outcome(None)


def _register(user: UUID) -> Steps:
    return [
        (CURRENT_USER, [str(user)]),
        (REGISTER, [user, hashlib.sha256(uuid4().bytes).digest()]),
    ]


def _deprovisioned(graph: RbacGraph, account: UUID) -> bool:
    with owner_context(graph.organization_a):
        return (
            not User.objects.get(pk=account).is_active
            and not (
                UserClinicRole.objects.filter(user_id=account, revoked_at__isnull=True)
            ).exists()
        )


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_p1_scim_and_a_sign_in_holding_the_session_key(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    # A sign-in holds the session key, then inserts its session row.
    resource, account, token = _directory_receptionist(rbac_graph)
    with psycopg.connect(superuser_database_url) as sign_in:
        sign_in.execute(SESSION_KEY, [account])
        scim = _deprovision(resource, token)
        scim.start_and_await_blocked(superuser_database_url)
        signed_in = _second_step(sign_in, _register(account))
    assert (signed_in, scim.outcome()) == ("completed", "status 200")
    assert _deprovisioned(rbac_graph, account)


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_p2_scim_and_a_gate_first_path(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    # Offboarding's first two steps: the user gate, then the user row.
    resource, account, token = _directory_receptionist(rbac_graph)
    with psycopg.connect(superuser_database_url) as gated:
        gated.execute(USER_GATE, [account])
        scim = _deprovision(resource, token)
        scim.start_and_await_blocked(superuser_database_url)
        locked = _second_step(
            gated,
            [
                (
                    "SELECT 1 FROM clinic_app.identity_user WHERE id = %s"
                    " FOR NO KEY UPDATE",
                    [account],
                )
            ],
        )
    assert (locked, scim.outcome()) == ("completed", "status 200")
    assert _deprovisioned(rbac_graph, account)


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_p3_scim_and_a_passkey_sign_in(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    # A passkey sign-in (passkey row, then its session row) queued first, SCIM
    # second, both behind a third connection's hold on the passkey row.
    resource, account, token = _directory_receptionist(rbac_graph)
    with psycopg.connect(superuser_database_url, autocommit=True) as setup:
        row = setup.execute(
            "INSERT INTO clinic_app.identity_passkey (id, organization_id, user_id,"
            " clinic_id, credential_id, public_key, sign_count, transports,"
            " created_at) VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, 0,"
            " '[]'::jsonb, statement_timestamp()) RETURNING id",
            [
                rbac_graph.organization_a,
                account,
                rbac_graph.clinic_a,
                uuid4().bytes,
                b"\x00",
            ],
        ).fetchone()
    assert row is not None
    passkey = row[0]
    lock_passkey = (
        "SELECT 1 FROM clinic_app.identity_passkey WHERE id = %s FOR NO KEY UPDATE"
    )
    signed_in: dict[str, str] = {}
    with (
        psycopg.connect(superuser_database_url) as gate,
        psycopg.connect(superuser_database_url) as signer,
    ):
        gate.execute(lock_passkey, [passkey])

        def sign_in() -> None:
            try:
                signer.execute(lock_passkey, [passkey])
                signed_in["outcome"] = _second_step(signer, _register(account))
            except psycopg.Error as error:
                signer.rollback()
                signed_in["outcome"] = _outcome(error)

        first = threading.Thread(target=sign_in)
        first.start()
        _await_blocked(superuser_database_url, signer.info.backend_pid)
        scim = _deprovision(resource, token)
        scim.start_and_await_blocked(superuser_database_url)
        gate.commit()
        first.join(timeout=60)
        assert not first.is_alive()
        deprovisioned = scim.outcome()
    assert (signed_in["outcome"], deprovisioned) == ("completed", "status 200")
    assert _deprovisioned(rbac_graph, account)


def test_p4_offboarding_and_a_sign_in_holding_the_session_key(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    target = _target(rbac_graph)

    def offboard() -> str:
        with tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a):
            offboard_staff(clinic_id=rbac_graph.clinic_b, user_id=target.pk)
        return "completed"

    with psycopg.connect(superuser_database_url) as sign_in:
        sign_in.execute(SESSION_KEY, [target.pk])
        offboarding = _Background(offboard)
        offboarding.start_and_await_blocked(superuser_database_url)
        signed_in = _second_step(sign_in, _register(target.pk))
    assert (signed_in, offboarding.outcome()) == ("completed", "completed")
    assert not User.objects.get(pk=target.pk).is_active
