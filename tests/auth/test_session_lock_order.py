"""Session locks follow one order per user and cannot deadlock (review S2).

A row is always locked before the user's session key
(``clinic-lock-v1:sessions:<user>``, identity 0056):

    identity_user row -> authenticator rows -> membership rows
    -> session key -> session rows

Each test interleaves two real paths that lock the same pair in opposite
orders before 0056:

1. connection H takes its first lock (a row) and holds it;
2. path P starts on a second connection and blocks on that row;
3. a monitor waits, in one server-side statement bounded by
   ``statement_timeout``, until PostgreSQL reports P blocked. That is a state
   wait, not a timer;
4. H runs its real second step, which takes the session key.

In the fixed order P waits for the row without holding the session key, so H
proceeds, commits and P completes. In the old order P already held the key,
the two form a cycle, and PostgreSQL aborts one with ``deadlock_detected``
(H's ``lock_timeout`` is longer than the 1 s deadlock detector).
"""

from __future__ import annotations

import hashlib
import threading
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.identity.models import User
from apps.identity.offboarding import offboard_staff
from apps.tenancy.db import tenant_context
from django.db import connection, connections
from django.test import override_settings
from psycopg import sql

from auth.passkey_test_support import registered_passkey
from identity.test_offboarding import _target
from otp_test_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

BLOCKED: sql.SQL = sql.SQL(
    "DO $wait$ BEGIN"
    " WHILE cardinality(pg_catalog.pg_blocking_pids({pid})) = 0 LOOP END LOOP;"
    " END $wait$"
)
SECOND_STEP_TIMEOUT = "SET LOCAL lock_timeout = '5s'"


def _await_blocked(url: str, pid: int) -> None:
    """Return once PostgreSQL reports backend ``pid`` waiting on a lock."""
    with psycopg.connect(url, autocommit=True) as monitor:
        monitor.execute("SET statement_timeout = '15s'")
        monitor.execute(BLOCKED.format(pid=sql.Literal(pid)))


def _outcome(error: BaseException | None) -> str:
    return "completed" if error is None else type(error).__name__ + f": {error}"


def test_password_change_and_offboarding_cannot_deadlock(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    # (A) A password change locks the user row, then (in its trigger) the
    # session key; offboarding must not hold that key while waiting for the row.
    target = _target(rbac_graph)
    offboarding: dict[str, object] = {}
    started = threading.Event()

    def offboard() -> None:
        try:
            with (
                runtime_role(),
                tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
            ):
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    row = cursor.fetchone()
                    assert row is not None
                    offboarding["pid"] = int(row[0])
                started.set()
                offboard_staff(clinic_id=rbac_graph.clinic_b, user_id=target.pk)
            offboarding["outcome"] = _outcome(None)
        except Exception as error:  # noqa: BLE001 - the outcome is the assertion
            offboarding["outcome"] = _outcome(error)
        finally:
            started.set()
            connections.close_all()

    with psycopg.connect(superuser_database_url) as password:
        password.execute(
            "SELECT 1 FROM clinic_app.identity_user WHERE id = %s FOR UPDATE",
            [target.pk],
        )
        worker = threading.Thread(target=offboard)
        worker.start()
        assert started.wait(timeout=30), "offboarding never started"
        assert "pid" in offboarding, offboarding
        _await_blocked(superuser_database_url, int(str(offboarding["pid"])))
        password.execute(SECOND_STEP_TIMEOUT)
        try:
            password.execute(
                "UPDATE clinic_app.identity_user SET password = password || 'x'"
                " WHERE id = %s",
                [target.pk],
            )
            password.commit()
            changed = _outcome(None)
        except psycopg.Error as error:
            password.rollback()
            changed = _outcome(error)
    worker.join(timeout=60)
    assert not worker.is_alive()
    assert (changed, offboarding["outcome"]) == ("completed", "completed")
    assert not User.objects.get(pk=target.pk).is_active


@override_settings(WEBAUTHN_RP_ID="testserver", WEBAUTHN_ORIGIN="http://testserver")
def test_passkey_sign_in_and_authenticator_retirement_cannot_deadlock(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    # (B) Passkey sign-in locks the passkey row, then registers the session
    # under the session key; retirement must not hold that key while waiting
    # for the passkey row.
    registered_passkey(rbac_graph)
    user: UUID = rbac_graph.clinic_admin
    retired: dict[str, str] = {}

    with (
        psycopg.connect(superuser_database_url) as sign_in,
        psycopg.connect(superuser_database_url) as retirement,
    ):
        locked = sign_in.execute(
            "SELECT 1 FROM clinic_app.identity_passkey"
            " WHERE user_id = %s AND removed_at IS NULL FOR UPDATE",
            [user],
        ).fetchall()
        assert locked, "the sign-in needs a live passkey"

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
        sign_in.execute(SECOND_STEP_TIMEOUT)
        sign_in.execute(
            "SELECT set_config('app.current_user_id', %s, true)", [str(user)]
        )
        try:
            sign_in.execute(
                "SELECT clinic_app.identity_session_register(%s, %s,"
                " '127.0.0.0/24', 'other', statement_timestamp() + interval '1 hour')",
                [user, hashlib.sha256(uuid4().bytes).digest()],
            )
            sign_in.commit()
            signed_in = _outcome(None)
        except psycopg.Error as error:
            sign_in.rollback()
            signed_in = _outcome(error)
        worker.join(timeout=60)
        assert not worker.is_alive()
    assert (signed_in, retired["outcome"]) == ("completed", "completed")
