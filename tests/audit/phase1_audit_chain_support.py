"""Fixed-matrix, concurrency, rollback, and reversal audit assertions."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

import psycopg
import pytest
from apps.audit.canonical import AuditEventInput, AuditTrustedContext
from apps.audit.events import PHASE1_AUDIT_EVENTS, build_phase1_audit_event
from apps.audit.models import SYSTEM_ORG_ID
from apps.audit.services import _record_system_event, record_event, verify_chain
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from psycopg.errors import InvalidParameterValue

from audit.phase1_audit_sql_support import (
    ACTOR_ID,
    AUDIT_V1,
    AUDIT_V2,
    CLINIC_ID,
    TENANT_ID,
    raw_append,
    sequence_state,
    set_context,
)


def tenant_matrix_count() -> int:
    """Count the fixed-matrix events that append to the tenant chain."""
    return sum(
        1
        for event_type in PHASE1_AUDIT_EVENTS
        if build_phase1_audit_event(
            event_type, clinic_id=CLINIC_ID, affected_record_id=SYSTEM_ORG_ID
        ).chain
        == "tenant"
    )


def system_matrix_count() -> int:
    """Count the fixed-matrix events that append to the system chain."""
    return sum(
        1
        for event_type in PHASE1_AUDIT_EVENTS
        if build_phase1_audit_event(
            event_type, clinic_id=CLINIC_ID, affected_record_id=SYSTEM_ORG_ID
        ).chain
        == "system"
    )


def append_fixed_matrix() -> None:
    tenant_seqs: list[int] = []
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_app")
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true), "
            "set_config('app.current_user_id', %s, true)",
            [str(TENANT_ID), str(ACTOR_ID)],
        )
        for position, event_type in enumerate(PHASE1_AUDIT_EVENTS, start=1):
            append = build_phase1_audit_event(
                event_type,
                clinic_id=CLINIC_ID,
                affected_record_id=UUID(f"55555555-5555-4555-8555-{position:012d}"),
            )
            if append.chain == "tenant":
                tenant_seqs.append(record_event(append.event, payload=append.payload))
        cursor.execute("RESET ROLE")
    system_seqs: list[int] = []
    for position, event_type in enumerate(PHASE1_AUDIT_EVENTS, start=1):
        append = build_phase1_audit_event(
            event_type,
            clinic_id=CLINIC_ID,
            affected_record_id=UUID(f"66666666-6666-4666-8666-{position:012d}"),
        )
        if append.chain == "system":
            system_seqs.append(
                _record_system_event(append.event, payload=append.payload)
            )
    assert len(tenant_seqs) == tenant_matrix_count()
    assert all(seq > 0 for seq in system_seqs)
    _assert_matrix_rows()


def _assert_matrix_rows() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT event_type, component_id, component_ip, "
            "affected_record_type, affected_record_id, payload "
            "FROM clinic_app.audit_event WHERE event_type = ANY(%s) "
            "ORDER BY event_type",
            [list(PHASE1_AUDIT_EVENTS)],
        )
        rows = cursor.fetchall()
    assert len(rows) == len(PHASE1_AUDIT_EVENTS)
    for row in rows:
        definition = PHASE1_AUDIT_EVENTS[str(row[0])]
        assert row[1:4] == (
            definition.component_id,
            None,
            definition.affected_record_type,
        )
        assert row[4] is not None
        assert json.loads(row[5]) == {
            "clinic_id": str(CLINIC_ID),
            "object_verb": definition.object_verb,
        }


def append_concurrently(app_database_url: str) -> list[int]:
    barrier = Barrier(3)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            _concurrent_append,
            app_database_url,
            barrier,
            "scheduling.appointment.viewed",
            UUID("77777777-7777-4777-8777-777777777771"),
        )
        second = pool.submit(
            _concurrent_append,
            app_database_url,
            barrier,
            "scheduling.agenda.viewed",
            UUID("77777777-7777-4777-8777-777777777772"),
        )
        barrier.wait(timeout=3)
        return sorted([first.result(timeout=3), second.result(timeout=3)])


def _concurrent_append(
    app_database_url: str,
    barrier: Barrier,
    event_type: str,
    affected_record_id: UUID,
) -> int:
    append = build_phase1_audit_event(
        event_type,
        clinic_id=CLINIC_ID,
        affected_record_id=affected_record_id,
    )
    with psycopg.connect(app_database_url) as raw_connection:
        set_context(raw_connection)
        barrier.wait(timeout=3)
        return raw_append(
            raw_connection,
            "audit_append",
            append.event,
            append.payload,
            AuditTrustedContext(TENANT_ID, ACTOR_ID),
        )


def verify_new_chains() -> tuple[int, int | None]:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_app")
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true), "
            "set_config('app.current_user_id', %s, true)",
            [str(TENANT_ID), str(ACTOR_ID)],
        )
        tenant_result = verify_chain(TENANT_ID)
        cursor.execute("RESET ROLE")
    system_result = verify_chain()
    # The v1 seed row, the fixed matrix and the two concurrent appends.
    assert tenant_result.row_count == tenant_matrix_count() + 3
    assert system_result.row_count == system_matrix_count()
    return tenant_result.row_count, tenant_result.last_seq


def assert_rollback_gap(app_database_url: str, expected_tip: int | None) -> None:
    append = build_phase1_audit_event(
        "scheduling.availability.viewed",
        clinic_id=CLINIC_ID,
        affected_record_id=UUID("88888888-8888-4888-8888-888888888888"),
    )
    with psycopg.connect(app_database_url) as raw_connection:
        set_context(raw_connection)
        aborted_seq = raw_append(
            raw_connection,
            "audit_append",
            append.event,
            append.payload,
            AuditTrustedContext(TENANT_ID, ACTOR_ID),
        )
        raw_connection.rollback()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*), max(seq) FROM clinic_app.audit_event "
            "WHERE organization_id = %s",
            [TENANT_ID],
        )
        count_and_tip = cursor.fetchone()
    assert count_and_tip == (tenant_matrix_count() + 3, expected_tip)
    assert expected_tip is not None
    assert aborted_seq > expected_tip
    assert sequence_state()[0] >= aborted_seq


def historical_rows() -> list[tuple[int, bytes]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT seq, curr_hash FROM clinic_app.audit_event "
            "WHERE organization_id IN (%s, %s) ORDER BY seq",
            [TENANT_ID, SYSTEM_ORG_ID],
        )
        return [(int(row[0]), bytes(row[1])) for row in cursor.fetchall()]


def assert_reverse_reapply(
    app_database_url: str,
    old_event: AuditEventInput,
    old_seq: int,
    expected_rows: list[tuple[int, bytes]],
) -> None:
    MigrationExecutor(connection).migrate([AUDIT_V1])
    with psycopg.connect(app_database_url) as raw_connection:
        set_context(raw_connection)
        with pytest.raises(InvalidParameterValue):
            raw_append(
                raw_connection,
                "audit_append",
                old_event,
                {"clinic_id": str(CLINIC_ID)},
                AuditTrustedContext(TENANT_ID, ACTOR_ID),
            )
        raw_connection.rollback()
    executor = MigrationExecutor(connection)
    executor.migrate([AUDIT_V2])
    assert historical_rows() == expected_rows
    assert old_seq == expected_rows[0][0]
