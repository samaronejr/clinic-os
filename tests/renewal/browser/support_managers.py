"""Shared synthetic manager fixtures without browser-test import dependencies."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from uuid import uuid4

import psycopg
from django.contrib.auth.hashers import make_password

from renewal.browser._fixture_secrets import new_totp_key
from renewal.browser._totp_devices import seed_totp_device


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
        seed_totp_device(conn, manager["id"], manager["totp_key"])
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


def set_ai_switches(
    staff: dict[str, str],
    actor: str,
    *,
    global_enabled: bool,
    organization_enabled: bool,
) -> dict[str, bool]:
    """Set the global AI switch and the organization opt-in as the owner would.

    ``actor`` publishes the organization policy; the receipt is the state the
    runtime role reads back after both writes.
    """
    environment = {
        **os.environ,
        "APP_DATABASE_URL": staff["dsn"],
        "DJANGO_SETTINGS_MODULE": "config.settings.base",
        "CLINIC_DATA_MODE": "synthetic",
        "CELERY_BROKER_URL": "memory://",
        "PYTHONPATH": os.pathsep.join((".", "tests")),
    }
    result = subprocess.run(  # noqa: S603 - fixed fixture entrypoint, owned DSN
        [
            sys.executable,
            "-c",
            "import django; django.setup(); "
            "from renewal.ai_switch_fixture import main; main()",
            actor,
            staff["clinic_a"],
            staff["organization"],
            "on" if global_enabled else "off",
            "on" if organization_enabled else "off",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    state: dict[str, bool] = json.loads(result.stdout)
    return state
