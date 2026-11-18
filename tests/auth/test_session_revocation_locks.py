"""Every per-user session revocation waits for the user's session lock (S1).

``identity_session_rotate`` registers a rotated key under
``pg_advisory_xact_lock(hashtextextended('clinic-lock-v1:sessions:' || user, 0))``
(identity 0056; the lock is taken after every row lock, see
``test_session_lock_order.py``).
A revocation that does not take the same key can run while a rotation is in
flight and miss the key it registers. Each test holds that key for one user on
a second connection, the way an uncommitted rotation does, and then runs one
revocation path for the user under ``lock_timeout``. The path must wait for the
key: it fails with ``LockNotAvailable`` instead of completing. That bound ends
the wait deterministically, because the held key is never released while the
statement runs; no test sleeps.

Both connections are raw superuser connections, so row security and grants
cannot mask the result; every trigger still fires.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

USER_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"
CURRENT_USER = "SELECT set_config('app.current_user_id', %s, true)"
Statement = tuple[str, "Sequence[object]"]


@contextmanager
def _rotation_in_flight(url: str, user_id: UUID) -> Iterator[None]:
    """Hold the user's session lock, as an uncommitted rotation does."""
    with psycopg.connect(url) as holder:
        holder.execute(USER_LOCK, [f"clinic-lock-v1:sessions:{user_id}"])
        try:
            yield
        finally:
            holder.rollback()


def _waits_for_rotation(url: str, statements: Sequence[Statement]) -> bool:
    """Run ``statements`` in one transaction; True when the last one waited."""
    with psycopg.connect(url) as actor:
        try:
            actor.execute("SET LOCAL lock_timeout = '1s'")
            for statement, parameters in statements[:-1]:
                actor.execute(statement, parameters)
            statement, parameters = statements[-1]
            try:
                actor.execute(statement, parameters)
            except psycopg.errors.LockNotAvailable:
                return True
            return False
        finally:
            actor.rollback()


def _held(url: str, user_id: UUID, statements: Sequence[Statement]) -> bool:
    with _rotation_in_flight(url, user_id):
        return _waits_for_rotation(url, statements)


def test_password_change_waits_for_a_rotation(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    user = rbac_graph.physician
    statement = (
        "UPDATE clinic_app.identity_user SET password = password || 'x' WHERE id = %s",
        [user],
    )
    assert _held(superuser_database_url, user, [statement])


def test_deactivation_waits_for_a_rotation(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    user = rbac_graph.physician
    statement = (
        "UPDATE clinic_app.identity_user SET is_active = false WHERE id = %s",
        [user],
    )
    assert _held(superuser_database_url, user, [statement])


def test_authenticator_retirement_waits_for_a_rotation(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    user = rbac_graph.physician
    statement = ("SELECT clinic_app.identity_retire_authenticators(%s)", [user])
    assert _held(superuser_database_url, user, [statement])


def test_role_grant_change_waits_for_every_member(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    # A grant is append-only (a "remove" rule); adding one revokes every member
    # of (clinic, role), here the physician.
    member = rbac_graph.physician
    statement = (
        "INSERT INTO clinic_app.identity_rolegrant (id, role, permission,"
        " bundle_version, effect, valid_from, clinic_id, organization_id)"
        " VALUES (gen_random_uuid(), 'physician', 'charge.read', 1, 'remove',"
        " statement_timestamp(), %s, %s)",
        [rbac_graph.clinic_a, rbac_graph.organization_a],
    )
    assert _held(superuser_database_url, member, [statement])


def test_membership_reassignment_waits_for_the_previous_holder(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    # Reassigning a membership revokes the old holder's sessions too.
    previous, successor = rbac_graph.physician, rbac_graph.clinic_admin
    statement = (
        "UPDATE clinic_app.identity_userclinicrole SET user_id = %s"
        " WHERE user_id = %s AND clinic_id = %s AND revoked_at IS NULL",
        [successor, previous, rbac_graph.clinic_a],
    )
    assert _held(superuser_database_url, previous, [statement])


def test_recovery_code_use_waits_for_a_rotation(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    user = rbac_graph.physician
    statements: list[Statement] = [
        (CURRENT_USER, [str(user)]),
        (
            "SELECT clinic_app.identity_recovery_consume(%s)",
            [hashlib.sha256(uuid4().bytes).digest()],
        ),
    ]
    assert _held(superuser_database_url, user, statements)


def test_first_session_registration_waits_for_a_rotation(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    user = rbac_graph.physician
    statements: list[Statement] = [
        (CURRENT_USER, [str(user)]),
        (
            "SELECT clinic_app.identity_session_register(%s, %s, '127.0.0.0/24',"
            " 'other', statement_timestamp() + interval '1 hour')",
            [user, hashlib.sha256(uuid4().bytes).digest()],
        ),
    ]
    assert _held(superuser_database_url, user, statements)


def test_the_retired_two_step_rotation_is_not_executable_by_the_runtime(
    app_database_url: str,
) -> None:
    with (
        psycopg.connect(app_database_url) as runtime,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        runtime.execute(
            "SELECT clinic_app.identity_session_rotate_hash(%s)",
            [hashlib.sha256(uuid4().bytes).digest()],
        )
