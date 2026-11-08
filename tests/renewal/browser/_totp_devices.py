"""Fixture access to staff authenticators through the TOTP boundary.

Since identity 0050 the device secret lives in ``identity_totpsecret``, which
only ``clinic_resolver`` can read or write, and a device-table trigger refuses
any other writer's change to the verification state (secret column, replay
counter, drift, use stamp, confirmation). A fixture that needs a known
authenticator therefore seeds, resets and reads it as ``clinic_resolver`` (the
fixture owner holds SET on that role) and then returns to whatever role the
caller held, so the rest of its transaction runs unchanged. Codes typed into
the browser are then checked by the same in-database verifier real users hit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from psycopg import sql

from renewal.browser._fixture_secrets import FixtureSecret

if TYPE_CHECKING:
    import psycopg


def _switch_role(conn: psycopg.Connection[Any], role: str) -> str:
    """Set ``role`` for this session and return the role it replaced."""
    row = conn.execute("SELECT current_user").fetchone()
    assert row is not None
    conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
    return str(row[0])


def seed_totp_device(conn: psycopg.Connection[Any], user_id: str, key: str) -> None:
    """Add a confirmed authenticator for ``user_id`` holding the hex ``key``."""
    previous = _switch_role(conn, "clinic_resolver")
    row = conn.execute(
        "INSERT INTO clinic_app.otp_totp_totpdevice "
        "(name, confirmed, key, step, t0, digits, tolerance, drift, last_t, "
        "user_id, throttling_failure_count, created_at) "
        "VALUES ('Clinic OS authenticator', true, '', 30, 0, 6, 1, 0, -1, "
        "%s, 0, now()) RETURNING id",
        [user_id],
    ).fetchone()
    assert row is not None
    conn.execute(
        "INSERT INTO clinic_app.identity_totpsecret (device_id, secret) "
        "VALUES (%s, decode(%s, 'hex'))",
        [row[0], key],
    )
    _switch_role(conn, previous)


def reset_totp_device(
    conn: psycopg.Connection[Any], user_id: str, key: str | None = None
) -> None:
    """Clear the replay marker and drift of ``user_id``'s authenticators.

    With ``key`` the secret is also replaced, so an independent scene signs in
    with a fresh seed instead of waiting for the next 30-second window.
    """
    previous = _switch_role(conn, "clinic_resolver")
    if key is not None:
        conn.execute(
            "UPDATE clinic_app.identity_totpsecret SET secret = decode(%s, 'hex') "
            "WHERE device_id IN (SELECT id FROM clinic_app.otp_totp_totpdevice "
            "WHERE user_id = %s)",
            [key, user_id],
        )
    conn.execute(
        "UPDATE clinic_app.otp_totp_totpdevice SET last_t = -1, drift = 0 "
        "WHERE user_id = %s",
        [user_id],
    )
    _switch_role(conn, previous)


def totp_secret(conn: psycopg.Connection[Any], user_id: str) -> FixtureSecret:
    """Return the hex secret of ``user_id``'s newest authenticator."""
    previous = _switch_role(conn, "clinic_resolver")
    row = conn.execute(
        "SELECT encode(store.secret, 'hex') "
        "FROM clinic_app.identity_totpsecret AS store "
        "JOIN clinic_app.otp_totp_totpdevice AS device "
        "ON device.id = store.device_id "
        "WHERE device.user_id = %s ORDER BY device.id DESC LIMIT 1",
        [user_id],
    ).fetchone()
    _switch_role(conn, previous)
    assert row is not None
    return FixtureSecret(str(row[0]))
