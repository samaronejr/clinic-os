"""Teleconsult v2 fixture helpers shared by the video suites (owner DSN only).

Rooms are opaque: the stored room name is read back and checked against the
format and the session identifier instead of being derived from the session.
The v2 participant controls require ``clinical.write``, so the assigned
physician gets a current synthetic professional registration, written through
the protected-field envelope exactly like a runtime write.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg

from renewal.browser._protected import encrypt

OPAQUE_ROOM = re.compile(r"tc-[0-9a-f]{32}")


def _connect(staff: dict[str, str]) -> psycopg.Connection[tuple[object, ...]]:
    conn = psycopg.connect(staff["dsn"])
    conn.execute(
        "SELECT set_config('app.current_tenant', %s, true)", [staff["organization"]]
    )
    return conn


def stored_room_name(staff: dict[str, str], session_id: str) -> str:
    """Return the session's opaque room name; it never spells the session."""
    with _connect(staff) as conn:
        row = conn.execute(
            "SELECT room_name FROM clinic_app.teleconsult_teleconsultroom "
            "WHERE session_id=%s",
            [session_id],
        ).fetchone()
    assert row is not None
    name = str(row[0])
    assert OPAQUE_ROOM.fullmatch(name), name
    assert session_id not in name
    assert session_id.replace("-", "") not in name
    return name


def register_physician(staff: dict[str, str], physician_id: str | None = None) -> None:
    """Give the physician a current synthetic registration in clinic A, once."""
    user = physician_id or staff["physician_a_id"]
    now = datetime.now(UTC)
    with _connect(staff) as conn:
        exists = conn.execute(
            "SELECT 1 FROM clinic_app.identity_professionalregistration "
            "WHERE user_id=%s AND clinic_id=%s AND revoked_at IS NULL",
            [user, staff["clinic_a"]],
        ).fetchone()
        if exists is not None:
            return
        uf = conn.execute(
            "SELECT crm_uf FROM clinic_app.identity_clinic WHERE id=%s",
            [staff["clinic_a"]],
        ).fetchone()
        assert uf is not None
        conn.execute(
            "INSERT INTO clinic_app.identity_professionalregistration "
            "(id,organization_id,clinic_id,user_id,role,council,number,jurisdiction,"
            "specialty,synthetic,status,valid_from,valid_to) "
            "VALUES (%s,%s,%s,%s,'physician','CRM',%s,%s,%s,true,'regular',%s,%s)",
            [
                str(uuid4()),
                staff["organization"],
                staff["clinic_a"],
                user,
                encrypt(conn, "identity.registration.number", b"SINTETICO-037"),
                uf[0],
                encrypt(conn, "identity.registration.specialty", b"Sintetico"),
                now - timedelta(days=1),
                now + timedelta(days=1),
            ],
        )


def session_events(staff: dict[str, str], session_id: str) -> list[tuple[str, str]]:
    """Return the stored event history as (kind, actor_role) pairs."""
    with _connect(staff) as conn:
        return [
            (str(kind), str(role))
            for kind, role in conn.execute(
                "SELECT kind,actor_role FROM clinic_app.teleconsult_teleconsultevent "
                "WHERE session_id=%s ORDER BY created_at,id",
                [session_id],
            ).fetchall()
        ]


def device_checks(staff: dict[str, str], session_id: str) -> list[tuple[str, ...]]:
    """Return every stored device check's closed codes, oldest first."""
    with _connect(staff) as conn:
        return [
            tuple(str(value) for value in row)
            for row in conn.execute(
                "SELECT role,camera,microphone,speaker,network "
                "FROM clinic_app.teleconsult_teleconsultdevicecheck "
                "WHERE session_id=%s ORDER BY created_at,id",
                [session_id],
            ).fetchall()
        ]
