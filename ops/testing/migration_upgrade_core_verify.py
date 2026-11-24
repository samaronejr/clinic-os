"""Independently verify native core fixture inputs and relational semantics."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
from psycopg import errors

from ops.testing.migration_upgrade_core_fixture import (
    APPOINTMENT_END,
    APPOINTMENT_START,
    AVAILABILITY_END,
    AVAILABILITY_START,
    CLINIC_NAME,
    CLINIC_REGION,
    CLINIC_TIMEZONE,
    ORGANIZATION_CNPJ,
    ORGANIZATION_NAME,
    OWNER_USERNAME,
    PATIENT_BIRTH_DATE,
    PATIENT_NAME,
    PRACTITIONER_USERNAME,
)

if TYPE_CHECKING:
    from psycopg import Cursor


def _organization(cursor: Cursor[tuple[object, ...]]) -> object:
    cursor.execute(
        "SELECT id FROM clinic_app.identity_organization WHERE cnpj=%s AND name=%s",
        [ORGANIZATION_CNPJ, ORGANIZATION_NAME],
    )
    matches = cursor.fetchall()
    return matches[0][0] if len(matches) == 1 else None


def _patient(cursor: Cursor[tuple[object, ...]], organization: object) -> None:
    kek = (
        (Path(os.environ["CLINIC_SECRET_DIR"]) / "tenant-kek.secret")
        .read_text(encoding="ascii")
        .strip()
    )
    cursor.execute(
        "SELECT set_config('app.current_tenant', %s, true)", [str(organization)]
    )
    cursor.execute(
        """SELECT p.id, p.birth_date, p.full_name
        FROM clinic_app.intake_patient p WHERE p.organization_id=%s::uuid""",
        [str(organization)],
    )
    patients = cursor.fetchall()
    if len(patients) != 1:
        message = "core fixture mismatch: intake_patient row count"
        raise RuntimeError(message)
    _, birth, name = patients[0]
    try:
        cursor.execute(
            """SELECT
            clinic_app.tenant_decrypt(%s,'intake.patient.birth_date',%s) = %s,
            clinic_app.tenant_decrypt(%s,'intake.patient.full_name',%s) = %s""",
            [
                kek,
                birth,
                PATIENT_BIRTH_DATE.isoformat().encode(),
                kek,
                name,
                PATIENT_NAME.encode(),
            ],
        )
        valid = cursor.fetchone() == (True, True)
    except errors.DatabaseError as error:
        if error.sqlstate not in {"TEN01", "TEN02", "TEN03", "39000"}:
            raise
        valid = False
    if not valid:
        message = "core fixture mismatch: intake_patient envelope plaintext"
        raise RuntimeError(message)


def verify_core_fixture() -> list[str]:
    """Use seed literals and natural keys, not observed names or receipt IDs."""
    failures = []
    with (
        psycopg.connect(os.environ["UPGRADE_ADMIN_DATABASE_URL"]) as admin,
        admin.cursor() as cursor,
    ):
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cursor.execute("SELECT count(*) FROM clinic_app.identity_organization")
        if cursor.fetchone() != (1,):
            failures.append("core fixture mismatch: identity_organization row count")
        organization = _organization(cursor)
        if organization is None:
            return [*failures, "core fixture mismatch: identity_organization.name"]
        _patient(cursor, organization)
        cursor.execute(
            """
            SELECT count(*) FROM clinic_app.identity_clinic c
            JOIN clinic_app.identity_userclinicrole r ON r.clinic_id=c.id
            JOIN clinic_app.identity_user u ON u.id=r.user_id
            WHERE c.organization_id=%s AND c.name=%s AND c.crm_uf=%s AND c.timezone=%s
            AND (r.role,u.username) IN (('owner',%s),('physician',%s))
            """,
            [
                str(organization),
                CLINIC_NAME,
                CLINIC_REGION,
                CLINIC_TIMEZONE,
                OWNER_USERNAME,
                PRACTITIONER_USERNAME,
            ],
        )
        if cursor.fetchone() != (2,):
            failures.append("core fixture mismatch: clinic or membership natural key")
        cursor.execute(
            """
            SELECT count(*) FROM clinic_app.scheduling_appointment a
            JOIN clinic_app.identity_clinic c ON c.id=a.clinic_id
            JOIN clinic_app.identity_user u ON u.id=a.practitioner_id
            JOIN clinic_app.scheduling_availabilityblock b
              ON b.organization_id=a.organization_id AND b.clinic_id=c.id
              AND b.practitioner_id=u.id
            WHERE c.name=%s AND u.username=%s AND b.retired_at IS NULL
            AND a.start_at AT TIME ZONE c.timezone = %s::timestamp
            AND a.end_at AT TIME ZONE c.timezone = %s::timestamp
            AND b.start_at AT TIME ZONE c.timezone = %s::timestamp
            AND b.end_at AT TIME ZONE c.timezone = %s::timestamp
            AND b.created_at <= a.created_at
            """,
            [
                CLINIC_NAME,
                PRACTITIONER_USERNAME,
                APPOINTMENT_START,
                APPOINTMENT_END,
                AVAILABILITY_START,
                AVAILABILITY_END,
            ],
        )
        if cursor.fetchone() != (1,):
            failures.append("core fixture mismatch: appointment linkage or sequence")
    return failures
