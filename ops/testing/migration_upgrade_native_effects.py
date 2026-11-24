"""Independent checks for native writes outside the fixture recipe."""

from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from ops.testing.migration_upgrade_core_fixture import (
    CLINIC_NAME,
    ORGANIZATION_CNPJ,
    PATIENT_BIRTH_DATE,
    PATIENT_NAME,
)
from ops.testing.migration_upgrade_oracle_db import oracle_cursor

if TYPE_CHECKING:
    from collections.abc import Collection

    from psycopg import Connection, Cursor

_PLANS = ("core_operations", "intelligence_usage", "network_governance")
_QUOTAS = {
    "ai_tokens_in": 10_000_000,
    "ai_tokens_out": 10_000_000,
    "ai_tokens_cached": 10_000_000,
    "ai_audio_seconds": 360_000,
    "ai_cost_micros": 1_000_000_000,
    "video_seconds": 360_000,
    "messages": 10_000,
    "storage_bytes": 10_737_418_240,
    "seats": 10_000,
}


def _subscriptions(
    cursor: Cursor[tuple[object, ...]], required: Collection[str]
) -> list[str]:
    failures = []
    cursor.execute(
        "SELECT code,version,provisional,monthly_price_minor "
        "FROM clinic_app.subscriptions_plan"
    )
    if sorted(cursor.fetchall()) != [(code, 1, True, None) for code in _PLANS]:
        failures.append("native fixture: subscriptions_plan")
    cursor.execute(
        "SELECT p.code,e.meter,e.enabled,e.quota "
        "FROM clinic_app.subscriptions_entitlement e "
        "JOIN clinic_app.subscriptions_plan p ON p.id=e.plan_id"
    )
    expected = [
        (code, meter, code != "core_operations" or not meter.startswith("ai_"), quota)
        for code in _PLANS
        for meter, quota in sorted(_QUOTAS.items())
    ]
    if sorted(cursor.fetchall()) != expected:
        failures.append("native fixture: subscriptions_entitlement")
    cursor.execute(
        """SELECT p.code,s.state,s.revision,s.grace_ends_at,s.cancellation_at,
            s.trial_ends_at=s.created_at+interval '30 days',s.organization_id::text
        FROM clinic_app.subscriptions_subscription s
        JOIN clinic_app.subscriptions_plan p ON p.id=s.plan_id"""
    )
    rows = cursor.fetchall()
    if len(rows) != 1 or rows[0][:6] != (
        "intelligence_usage",
        "trial",
        1,
        None,
        None,
        True,
    ):
        failures.append("native fixture: subscriptions_subscription")
    cursor.execute(
        """SELECT e.action,e.plan_code,e.from_state,e.to_state,e.revision,
           e.command_id=s.id AND e.organization_id=s.organization_id
           AND e.occurred_at=s.created_at
        FROM clinic_app.subscriptions_subscriptionevent e
        JOIN clinic_app.subscriptions_subscription s ON s.id=e.subscription_id"""
    )
    if cursor.fetchall() != [
        ("trial_started", "intelligence_usage", "", "trial", 1, True)
    ]:
        failures.append("native fixture: subscriptions_subscriptionevent")
    cursor.execute(
        """SELECT m.role,u.username,s.organization_id=m.organization_id
           AND s.organization_id=sub.organization_id AND s.created_at>=sub.created_at
        FROM clinic_app.subscriptions_seat s
        JOIN clinic_app.subscriptions_subscription sub ON sub.id=s.subscription_id
        JOIN clinic_app.identity_userclinicrole m ON m.id=s.membership_id
        JOIN clinic_app.identity_user u ON u.id=m.user_id"""
    )
    seats = cursor.fetchall()
    expected_roles = ["owner", "physician"]
    if "inbox" in required:
        expected_roles.append("clinic_admin")
    if sorted(str(row[0]) for row in seats) != sorted(expected_roles) or not all(
        row[2] is True for row in seats
    ):
        failures.append("native fixture: subscriptions_seat")
    return failures


def _enrollment(
    cursor: Cursor[tuple[object, ...]], tenant: str, clinic: str
) -> list[str]:
    cursor.execute(
        """SELECT e.create_fingerprint,substring(e.idempotency_key::text,15,1),
           e.organization_id=p.organization_id AND e.created_at>=p.created_at
        FROM clinic_app.intake_patientclinicenrollment e
        JOIN clinic_app.intake_patient p ON p.id=e.patient_id WHERE e.clinic_id=%s""",
        [clinic],
    )
    payload = json.dumps(
        {
            "birth_date": PATIENT_BIRTH_DATE.isoformat(),
            "clinic_id": clinic,
            "full_name": PATIENT_NAME,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    failures = []
    if cursor.fetchall() != [
        (sha256(b"clinic-idempotency-v1\0" + payload).digest(), "4", True)
    ]:
        failures.append("native fixture: intake_patientclinicenrollment")
    kek = (
        (Path(os.environ["CLINIC_SECRET_DIR"]) / "tenant-kek.secret")
        .read_text()
        .strip()
    )
    cursor.execute(
        """SELECT d.version,d.index_key_version,d.contact_indexes=ARRAY[]::bytea[],
           d.name_birth_index=clinic_app.hmac(%s,
            clinic_app.tenant_dek_unwrap(%s,d.index_key_version),'sha256')
        FROM clinic_app.intake_patientduplicateindex d
        JOIN clinic_app.intake_patientclinicenrollment e ON e.patient_id=d.patient_id
        WHERE d.organization_id=%s AND d.clinic_id=e.clinic_id AND e.clinic_id=%s""",
        [b"intake.duplicate.name_birth.v1\01988-04-17:synthe", kek, tenant, clinic],
    )
    if cursor.fetchall() != [(1, 1, True, True)]:
        failures.append("native fixture: intake_patientduplicateindex")
    return failures


def verify_native_effects(
    required: Collection[str], *, database: Connection[tuple[object, ...]] | None = None
) -> list[str]:
    """Assert values and links of rows emitted by the native service path."""
    with oracle_cursor(database) as (_, cursor):
        cursor.execute(
            "SELECT o.id::text,c.id::text,"
            "set_config('app.current_tenant',o.id::text,true) "
            "FROM clinic_app.identity_organization o JOIN clinic_app.identity_clinic c "
            "ON c.organization_id=o.id WHERE o.cnpj=%s AND c.name=%s",
            [ORGANIZATION_CNPJ, CLINIC_NAME],
        )
        keys = cursor.fetchall()
        if len(keys) != 1:
            return ["native fixture: tenant and clinic identity"]
        tenant, clinic, _ = map(str, keys[0])
        failures = _enrollment(cursor, tenant, clinic)
        failures.extend(_subscriptions(cursor, required))
        cursor.execute(
            """SELECT e.event_type,e.component_id,e.component_ip,
                      e.payload->>'reason_code',e.payload->>'clinic_id'
            FROM clinic_app.audit_event e
            JOIN clinic_app.scheduling_appointment a ON e.affected_record_id=a.id::text
            WHERE e.affected_record_type='upgrade.seed' AND e.organization_id=%s
            AND a.clinic_id=%s ORDER BY e.seq""",
            [tenant, clinic],
        )
        if cursor.fetchall() != [
            (
                "upgrade.seed.marker",
                "migration-upgrade-gate",
                None,
                f"seed-marker-{i}",
                clinic,
            )
            for i in range(2)
        ]:
            failures.append("native fixture: audit_event")
        cursor.execute(
            """SELECT t.from_status,t.to_status,t.revision,t.command_id,t.actor_kind,
               t.occurred_at=a.transitioned_at AND t.clinic_id=a.clinic_id
               AND t.organization_id=a.organization_id
            FROM clinic_app.scheduling_appointmenttransition t
            JOIN clinic_app.scheduling_appointment a ON a.id=t.appointment_id
            WHERE a.clinic_id=%s""",
            [clinic],
        )
        transitions = cursor.fetchall()
        if transitions != [("", "scheduled", 1, None, "staff", True)] and (
            transitions or "ai" in required
        ):
            failures.append("native fixture: scheduling_appointmenttransition")
        if "ai" in required:
            cursor.execute(
                """SELECT r.clinic_id::text,r.organization_id::text,
                          r.created_at<=v.created_at
                FROM clinic_app.orders_result r
                JOIN clinic_app.orders_resultversion v ON v.result_id=r.id
                WHERE v.number=1"""
            )
            if cursor.fetchall() != [(clinic, tenant, True)]:
                failures.append("native fixture: orders_result")
    return failures
