"""Shared synthetic manager fixtures without browser-test import dependencies."""

from __future__ import annotations

import secrets
from uuid import uuid4

import psycopg
from django.contrib.auth.hashers import make_password

from renewal.browser._fixture_secrets import new_totp_key


def seed_manager(staff: dict[str, str]) -> dict[str, str]:
    """Seed one clinic admin with a confirmed authenticator for this run."""
    manager = {
        "username": f"gestora-{secrets.token_hex(4)}",
        "id": str(uuid4()),
        "totp_key": new_totp_key(),
    }
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        conn.execute(
            "INSERT INTO clinic_app.identity_user "
            "(id, username, password, email, first_name, last_name, is_active, "
            "is_staff, is_superuser, date_joined) "
            "VALUES (%s, %s, %s, '', '', '', true, false, false, now())",
            [manager["id"], manager["username"], make_password(staff["password"])],
        )
        conn.execute(
            "INSERT INTO clinic_app.identity_userclinicrole "
            "(id, user_id, organization_id, clinic_id, role, "
            "workflow_bundle_version) VALUES (%s, %s, %s, %s, 'clinic_admin', 1)",
            [str(uuid4()), manager["id"], staff["organization"], staff["clinic_a"]],
        )
        conn.execute("SET ROLE clinic_app")
        conn.execute(
            "SELECT set_config('app.current_user_id', %s, true)", [manager["id"]]
        )
        conn.execute(
            "INSERT INTO clinic_app.otp_totp_totpdevice "
            "(name, confirmed, key, step, t0, digits, tolerance, drift, last_t, "
            "user_id, throttling_failure_count, created_at) "
            "VALUES ('Clinic OS authenticator', true, %s, 30, 0, 6, 1, 0, -1, "
            "%s, 0, now())",
            [manager["totp_key"], manager["id"]],
        )
    return manager


def seed_settings_manager(staff: dict[str, str]) -> dict[str, str]:
    manager = seed_manager(staff)
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        conn.execute(
            "INSERT INTO clinic_app.identity_userclinicrole "
            "(id,user_id,organization_id,clinic_id,role,workflow_bundle_version) "
            "VALUES (%s,%s,%s,%s,'clinic_admin',1)",
            [str(uuid4()), manager["id"], staff["organization"], staff["clinic_b"]],
        )
    return manager


def seed_ai_admin(staff: dict[str, str], theme: str) -> dict[str, str]:
    """Seed an organization admin of both clinics who reads in ``theme``."""
    manager = seed_settings_manager(staff)
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)",
            [staff["organization"]],
        )
        for clinic in (staff["clinic_a"], staff["clinic_b"]):
            conn.execute(
                "INSERT INTO clinic_app.identity_userclinicrole "
                "(id,user_id,organization_id,clinic_id,role,workflow_bundle_version) "
                "VALUES (%s,%s,%s,%s,'org_admin',1)",
                [str(uuid4()), manager["id"], staff["organization"], clinic],
            )
        conn.execute("SET ROLE clinic_app")
        conn.execute(
            "SELECT set_config('app.current_user_id',%s,true)", [manager["id"]]
        )
        conn.execute(
            "INSERT INTO clinic_app.identity_userpreference "
            "(user_id,theme,density,updated_at) VALUES (%s,%s,'comfortable',now())",
            [manager["id"], theme],
        )
    return manager
