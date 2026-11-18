"""The end-of-request touch never waits, and nothing before it inverts (S6).

``touch_session`` runs last in every staff request transaction, after all of
that request's locks. Before review S6 it waited for the current session row,
so any earlier lock a concurrent revoker of the same user needed closed a
cycle:

- T1: TOTP enrollment locked the reused pending device ``FOR UPDATE``; a SCIM
  deprovisioning revokes the touched session row and, at commit, its retirement
  row's deferred foreign key needs ``FOR KEY SHARE`` on that device.
- T2: ``revoke_session`` locked another of the user's session rows without the
  session key, while a bulk revoker holds the key and the current row.

Now the touch locks its row ``FOR NO KEY UPDATE SKIP LOCKED`` and skips a held
row, enrollment locks the device ``FOR NO KEY UPDATE`` (identity 0060), and
``revoke_session`` takes the session key first. The style is
``test_lock_modes.py``: held first locks and server-side waits, no sleeps.
Where the fixed path no longer blocks at all, ``_start_settled`` waits until
the path is blocked or has finished.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.identity.models import User
from apps.identity.session_inventory import revoke_session, session_hash, touch_session
from apps.tenancy.db import tenant_context
from django.contrib.auth.hashers import make_password
from django.test import Client, override_settings
from django.urls import reverse
from psycopg import sql

from auth.test_directory_lock_order import _Directory, _directory_user
from auth.test_lock_modes import (
    SESSION_KEY,
    _Background,
    _deprovision,
    _deprovisioned,
    _second_step,
)
from auth.test_session_lock_order import _await_blocked
from otp_test_support import runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

LOCK_ROW = "SELECT 1 FROM clinic_app.identity_sessionrecord WHERE id = %s FOR UPDATE"
LIVE_ROW = (
    "SELECT id FROM clinic_app.identity_sessionrecord"
    " WHERE user_id = %s AND revoked_at IS NULL"
)
# Until the backend waits on a lock or is gone (the path finished and closed
# its connection); bounded by statement_timeout, no sleep.
SETTLED = sql.SQL("""
DO $settled$
BEGIN
    WHILE cardinality(pg_catalog.pg_blocking_pids({pid})) = 0
      AND EXISTS (SELECT 1 FROM pg_catalog.pg_stat_activity WHERE pid = {pid})
    LOOP
        PERFORM pg_catalog.pg_stat_clear_snapshot();
    END LOOP;
END $settled$
""")


def _start_settled(path: _Background, url: str) -> bool:
    """Start ``path``; return whether it is waiting on a lock once settled."""
    path.thread.start()
    assert path.started.wait(timeout=30), "the path never started"
    assert "pid" in path.result, path.result
    pid = int(str(path.result["pid"]))
    with psycopg.connect(url, autocommit=True) as monitor:
        monitor.execute("SET statement_timeout = '15s'")
        monitor.execute(SETTLED.format(pid=sql.Literal(pid)))
        row = monitor.execute(
            "SELECT cardinality(pg_catalog.pg_blocking_pids(%s)) > 0", [pid]
        ).fetchone()
    assert row is not None
    return bool(row[0])


def _register(url: str, user: UUID, key: str) -> UUID:
    with psycopg.connect(url, autocommit=True) as setup:
        setup.execute(
            "SELECT set_config('app.current_user_id', %s, false)", [str(user)]
        )
        setup.execute(
            "SELECT clinic_app.identity_session_register(%s, %s, '127.0.0.0/24',"
            " 'other', statement_timestamp() + interval '1 hour')",
            [user, session_hash(key)],
        )
        row = setup.execute(
            "SELECT id FROM clinic_app.identity_sessionrecord"
            " WHERE session_key_hash = %s",
            [session_hash(key)],
        ).fetchone()
    assert row is not None
    record: UUID = row[0]
    return record


def _session(url: str, record: UUID) -> tuple[datetime, datetime | None]:
    with psycopg.connect(url, autocommit=True) as look:
        row = look.execute(
            "SELECT last_seen, revoked_at FROM clinic_app.identity_sessionrecord"
            " WHERE id = %s",
            [record],
        ).fetchone()
    assert row is not None
    return row[0], row[1]


def _touch(graph: RbacGraph, key: str) -> str:
    with tenant_context(graph.clinic_admin, graph.organization_a):
        touch_session(session_key=key, http_status=200)
    return "completed"


def _enrolling(graph: RbacGraph, url: str) -> tuple[_Directory, Client, UUID]:
    """A signed-in directory physician with one pending TOTP device."""
    directory = _directory_user(graph, bundle="physician")
    with psycopg.connect(url, autocommit=True) as setup:
        setup.execute(
            "UPDATE clinic_app.identity_user SET password = %s WHERE id = %s",
            [make_password(RBAC_RAW_CREDENTIAL), directory.account],
        )
    username = User.objects.get(pk=directory.account).username
    client = Client()
    with runtime_role():
        assert client.login(username=username, password=RBAC_RAW_CREDENTIAL)
        assert client.post(reverse("identity:enroll"), {"action": "start"}).status_code
    with psycopg.connect(url, autocommit=True) as look:
        live = [row[0] for row in look.execute(LIVE_ROW, [directory.account])]
        pending = look.execute(
            "SELECT count(*) FROM clinic_app.otp_totp_totpdevice"
            " WHERE user_id = %s AND NOT confirmed",
            [directory.account],
        ).fetchone()
    assert (len(live), pending) == (1, (1,))
    return directory, client, live[0]


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_t1_enrollment_restart_and_scim_deprovisioning(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    # SCIM waits on the enrolling request's session row (held by H), then the
    # enrollment restart reuses the pending device and reaches the touch.
    directory, client, current = _enrolling(rbac_graph, superuser_database_url)

    def restart() -> str:
        response = client.post(reverse("identity:enroll"), {"action": "start"})
        return f"status {response.status_code}"

    with psycopg.connect(superuser_database_url) as holder:
        holder.execute(LOCK_ROW, [current])
        scim = _deprovision(directory.resource, directory.token)
        scim.start_and_await_blocked(superuser_database_url)
        enrollment = _Background(restart)
        _start_settled(enrollment, superuser_database_url)
        holder.commit()
    assert (scim.outcome(), enrollment.outcome()) == ("status 200", "status 200")
    assert _deprovisioned(rbac_graph, directory.account)


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_t1_enrollment_device_lock_and_scim_retirement(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    # The device lock alone: the real identity_totp_enroll holds the reused
    # pending device while SCIM retires it, then waits for the session key.
    directory, _client, _current = _enrolling(rbac_graph, superuser_database_url)
    with psycopg.connect(superuser_database_url) as enrolling:
        enrolling.execute(
            "SELECT set_config('app.current_user_id', %s, true)",
            [str(directory.account)],
        )
        reused = enrolling.execute(
            "SELECT count(*) FROM clinic_app.identity_totp_enroll()"
        ).fetchone()
        assert reused == (1,)
        scim = _deprovision(directory.resource, directory.token)
        _start_settled(scim, superuser_database_url)
        enrolled = _second_step(enrolling, [(SESSION_KEY, [directory.account])])
    assert (enrolled, scim.outcome()) == ("completed", "status 200")
    assert _deprovisioned(rbac_graph, directory.account)


def test_t2_revoking_another_session_and_a_password_change(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    # A password change revokes all the user's sessions under the session key,
    # the current row first (held by H); the inventory request revokes the
    # other row, then its touch reaches the current row.
    user = rbac_graph.clinic_admin
    current_key, other_key = uuid4().hex, uuid4().hex
    current = _register(superuser_database_url, user, current_key)
    other = _register(superuser_database_url, user, other_key)

    def revoke_other() -> str:
        with tenant_context(user, rbac_graph.organization_a):
            revoke_session(session_record_id=other)
            touch_session(session_key=current_key, http_status=200)
        return "completed"

    changed: dict[str, str] = {}
    with (
        psycopg.connect(superuser_database_url) as holder,
        psycopg.connect(superuser_database_url) as bulk,
    ):
        holder.execute(LOCK_ROW, [current])

        def change_password() -> None:
            changed["outcome"] = _second_step(
                bulk,
                [
                    (
                        "UPDATE clinic_app.identity_user"
                        " SET password = password || 'x' WHERE id = %s",
                        [user],
                    )
                ],
            )

        password = threading.Thread(target=change_password)
        password.start()
        _await_blocked(superuser_database_url, bulk.info.backend_pid)
        inventory = _Background(revoke_other)
        _start_settled(inventory, superuser_database_url)
        holder.commit()
        password.join(timeout=60)
        assert not password.is_alive()
    assert (changed["outcome"], inventory.outcome()) == ("completed", "completed")
    assert _session(superuser_database_url, current)[1] is not None
    assert _session(superuser_database_url, other)[1] is not None


def test_touch_skips_a_locked_row_and_never_touches_a_revoked_one(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    key = uuid4().hex
    record = _register(superuser_database_url, rbac_graph.clinic_admin, key)
    registered, _revoked = _session(superuser_database_url, record)
    with psycopg.connect(superuser_database_url) as holder:
        holder.execute(LOCK_ROW, [record])
        toucher = _Background(lambda: _touch(rbac_graph, key))
        waited = _start_settled(toucher, superuser_database_url)
        holder.commit()
    assert toucher.outcome() == "completed"
    assert not waited, "the touch waited for a held session row"
    assert _session(superuser_database_url, record)[0] == registered

    with runtime_role():
        _touch(rbac_graph, key)
    touched, _revoked = _session(superuser_database_url, record)
    assert touched > registered

    with psycopg.connect(superuser_database_url, autocommit=True) as revoke:
        revoke.execute(
            "UPDATE clinic_app.identity_sessionrecord"
            " SET revoked_at = statement_timestamp() WHERE id = %s",
            [record],
        )
    with runtime_role():
        _touch(rbac_graph, key)
    assert _session(superuser_database_url, record)[0] == touched
