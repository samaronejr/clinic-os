"""Two-approver agent proposals for sec.12 row 9 (todo 75 ``acceptance-scenarios``).

A quorum-2 proposal binds two independent reviewers. The scenario retains
both votes before revocation, then calls the real executor separately. A
first-vote refusal alone cannot prove execution-time authority revalidation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from uuid import UUID

import psycopg


def grant_org_admin(staff: dict[str, str], user_id: str) -> None:
    """Give ``user_id`` org_admin on every clinic the session's fixtures made."""
    with psycopg.connect(staff["dsn"]) as connection:
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        connection.execute(
            "INSERT INTO clinic_app.identity_userclinicrole (id, user_id, "
            "organization_id, clinic_id, role, workflow_bundle_version) "
            "SELECT gen_random_uuid(), %s, c.organization_id, c.id, 'org_admin', 1 "
            "FROM clinic_app.identity_clinic c WHERE c.organization_id = %s "
            "AND NOT EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole r "
            "WHERE r.user_id = %s AND r.clinic_id = c.id AND r.role = 'org_admin')",
            [user_id, staff["organization"], user_id],
        )


def seed_quorum_proposal(staff: dict[str, str], first: str, second: str) -> str:
    """Propose one validated action that needs both approvers' votes."""
    environment = {
        **os.environ,
        "APP_DATABASE_URL": staff["dsn"],
        "DJANGO_SETTINGS_MODULE": "config.settings.base",
        "CLINIC_DATA_MODE": "synthetic",
        "PYTHONPATH": ".:tests",
    }
    result = subprocess.run(  # noqa: S603 - fixed fixture module and opaque identifiers.
        [
            sys.executable,
            "-m",
            "renewal.agent_proposal_fixture",
            first,
            staff["clinic_a"],
            staff["organization"],
            second,
        ],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return str(UUID(json.loads(result.stdout)["proposal_id"]))


def revoke_clinic_roles(staff: dict[str, str], user_id: str) -> int:
    """Revoke every live role of ``user_id`` on clinic A (offboarding's fact)."""
    with psycopg.connect(staff["dsn"]) as connection:
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        return connection.execute(
            "UPDATE clinic_app.identity_userclinicrole SET revoked_at = now() "
            "WHERE user_id = %s AND clinic_id = %s AND revoked_at IS NULL",
            [user_id, staff["clinic_a"]],
        ).rowcount
