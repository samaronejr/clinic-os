"""Raw PostgreSQL helpers for the Phase 1A audit migration contract."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from uuid import UUID

import psycopg
import pytest
from apps.audit.canonical import (
    AuditEventInput,
    AuditPayloadValueRejectedError,
    AuditTrustedContext,
    _content_hash,
    _normalize_payload,
)
from apps.audit.events import build_phase1_audit_event
from apps.audit.services import record_event
from django.db import connection
from psycopg import sql
from psycopg.errors import InsufficientPrivilege, InvalidParameterValue

from database_urls import database_url_for_name

if TYPE_CHECKING:
    from collections.abc import Mapping

    from psycopg.rows import TupleRow

CLINIC_ID: Final = UUID("11111111-1111-4111-8111-111111111111")
TENANT_ID: Final = UUID("33333333-3333-4333-8333-333333333333")
ACTOR_ID: Final = UUID("44444444-4444-4444-8444-444444444444")
AUDIT_V1: Final = ("audit", "0004_raw_audit_string_boundaries")


def database_url(environment_name: str) -> str:
    return database_url_for_name(
        os.environ[environment_name], str(connection.settings_dict["NAME"])
    )


def set_context(raw_connection: psycopg.Connection[TupleRow]) -> None:
    raw_connection.execute(
        "SELECT set_config('app.current_tenant', %s, true), "
        "set_config('app.current_user_id', %s, true)",
        (str(TENANT_ID), str(ACTOR_ID)),
    )


def raw_append(
    raw_connection: psycopg.Connection[TupleRow],
    function_name: str,
    event: AuditEventInput,
    payload: Mapping[str, str],
    context: AuditTrustedContext,
) -> int:
    content_hash = _content_hash(event, context, _normalize_payload(payload))
    query = sql.SQL("SELECT clinic_app.{}(%s,%s,%s,%s,%s,%s,%s,%s)").format(
        sql.Identifier(function_name)
    )
    row = raw_connection.execute(
        query,
        (
            event.event_type,
            event.component_id,
            None,
            event.affected_record_type,
            event.affected_record_id,
            event.occurred_at_utc,
            json.dumps(dict(payload)),
            content_hash,
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


def sequence_state() -> tuple[int, bool]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT last_value, is_called FROM clinic_app.audit_event_seq_seq"
        )
        row = cursor.fetchone()
    assert row is not None
    return int(row[0]), bool(row[1])


def seed_v1(app_database_url: str) -> tuple[int, AuditEventInput]:
    event = AuditEventInput(
        event_type="audit.synthetic.legacy",
        component_id="legacy-safe-component",
        component_ip=None,
        affected_record_type=None,
        affected_record_id=None,
        occurred_at_utc=datetime.now(UTC),
    )
    with psycopg.connect(app_database_url) as raw_connection:
        set_context(raw_connection)
        seq = raw_append(
            raw_connection,
            "audit_append",
            event,
            {"reason_code": "legacy-safe-code"},
            AuditTrustedContext(TENANT_ID, ACTOR_ID),
        )
    return seq, event


def assert_v2_catalog() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT proname, pg_get_userbyid(proowner), prosecdef, proconfig, "
            "has_function_privilege('clinic_app', oid, 'EXECUTE'), "
            "has_function_privilege('clinic_resolver', oid, 'EXECUTE'), "
            "has_function_privilege('public', oid, 'EXECUTE') FROM pg_proc "
            "WHERE pronamespace = 'clinic_app'::regnamespace "
            "AND proname LIKE 'audit_append%' ORDER BY proname"
        )
        functions = cursor.fetchall()
        cursor.execute(
            "SELECT pg_get_functiondef(oid) FROM pg_proc "
            "WHERE pronamespace = 'clinic_app'::regnamespace "
            "AND proname IN ('audit_append_unchecked_v2', "
            "'audit_append_system_unchecked_v2') ORDER BY proname"
        )
        sources = [str(row[0]) for row in cursor.fetchall()]
    assert {row[0] for row in functions} == {
        "audit_append",
        "audit_append_raw_v1",
        "audit_append_system",
        "audit_append_system_raw_v1",
        "audit_append_system_unchecked_v1",
        "audit_append_system_unchecked_v2",
        "audit_append_unchecked_v1",
        "audit_append_unchecked_v2",
    }
    assert all(
        row[1:4] == ("clinic_owner", True, ["search_path=pg_catalog, pg_temp"])
        for row in functions
    )
    assert {row[0] for row in functions if row[4]} == {"audit_append"}
    assert all(not row[5] and not row[6] for row in functions)
    assert len(sources) == 2
    assert all(
        source.index("payload ->> 'clinic_id'") < source.index("INSERT INTO")
        for source in sources
    )


def assert_preallocation_rejections(
    app_database_url: str,
    owner_database_url: str,
) -> None:
    before = sequence_state()
    append = build_phase1_audit_event(
        "scheduling.agenda.viewed",
        clinic_id=CLINIC_ID,
        affected_record_id=CLINIC_ID,
    )
    with pytest.raises(AuditPayloadValueRejectedError):
        record_event(append.event, payload={"clinic_id": "not-a-canonical-uuid"})
    payloads: list[object] = [
        {"clinic_id": "11111111-1111-4111-8111-11111111111A"},
        {"clinic_id": 7},
        {"patient_name": "synthetic forbidden value"},
        {"birth_date": "synthetic forbidden value"},
        {"query": "synthetic forbidden value"},
        {"time": "synthetic forbidden value"},
        {"role": "synthetic forbidden value"},
        {"reason": "synthetic forbidden value"},
        {"free_text": "synthetic forbidden value"},
        {"patient_id": "synthetic forbidden value"},
        {"enrollment_id": "synthetic forbidden value"},
        {"clinic_id": {"nested": "synthetic forbidden value"}},
    ]
    for payload in payloads:
        with psycopg.connect(app_database_url) as raw_connection:
            set_context(raw_connection)
            with pytest.raises(InvalidParameterValue):
                raw_connection.execute(
                    "SELECT clinic_app.audit_append(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        "audit.synthetic",
                        "clinic-os-web",
                        None,
                        None,
                        None,
                        datetime.now(UTC),
                        json.dumps(payload),
                        b"x" * 32,
                    ),
                )
            raw_connection.rollback()
    _assert_function_access(app_database_url)
    _assert_system_input(owner_database_url)
    assert sequence_state() == before


def _assert_function_access(app_database_url: str) -> None:
    with psycopg.connect(app_database_url) as raw_connection:
        for function_name in (
            "audit_append_unchecked_v2",
            "audit_append_system",
            "audit_append_system_unchecked_v2",
        ):
            set_context(raw_connection)
            with pytest.raises(InsufficientPrivilege):
                raw_connection.execute(
                    sql.SQL("SELECT clinic_app.{}(%s,%s,%s,%s,%s,%s,%s,%s)").format(
                        sql.Identifier(function_name)
                    ),
                    (
                        "audit.synthetic",
                        "clinic-os-web",
                        None,
                        None,
                        None,
                        datetime.now(UTC),
                        "{}",
                        b"x" * 32,
                    ),
                )
            raw_connection.rollback()


def _assert_system_input(owner_database_url: str) -> None:
    with psycopg.connect(owner_database_url) as raw_connection:
        with pytest.raises(InvalidParameterValue):
            raw_connection.execute(
                "SELECT clinic_app.audit_append_system(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    "audit.synthetic",
                    "clinic-os-ops",
                    None,
                    None,
                    None,
                    datetime.now(UTC),
                    '{"clinic_id":"not-a-canonical-uuid"}',
                    b"x" * 32,
                ),
            )
        raw_connection.rollback()
