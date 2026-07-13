"""PostgreSQL audit append security integration specification.

# noqa: SIZE_OK
"""

from __future__ import annotations

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from ipaddress import ip_address
from queue import Queue
from time import monotonic
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import psycopg
import pytest
import rfc8785
from apps.audit.services import (
    AUDIT_PAYLOAD_ALLOWED,
    AuditEventInput,
    AuditPayloadKeyRejected,
    AuditPayloadValueRejectedError,
    AuditTrustedContext,
    CanonicalValue,
    _content_hash,
    _record_system_event,
    record_event,
)
from django.apps import apps as django_apps
from django.db import connection, transaction
from psycopg.errors import (
    InsufficientPrivilege,
    InvalidParameterValue,
)

from database_urls import database_url_for_name

if TYPE_CHECKING:
    from collections.abc import Mapping

    from psycopg.rows import TupleRow

SYSTEM_ORG_ID: Final = UUID("00000000-0000-0000-0000-000000000000")
ZERO_HASH: Final = b"\x00" * 32
APPEND_SIGNATURE: Final = (
    "clinic_app.audit_append(text,text,inet,text,text,timestamptz,jsonb,bytea)"
)
SYSTEM_APPEND_SIGNATURE: Final = (
    "clinic_app.audit_append_system(text,text,inet,text,text,timestamptz,jsonb,bytea)"
)


@dataclass(frozen=True, slots=True)
class DirectAppend:
    event_type: str = "audit.synthetic"
    component_id: str = "test-suite"
    component_ip: str | None = "127.0.0.1"
    affected_record_type: str | None = None
    affected_record_id: str | None = None
    occurred_at_utc: datetime | None = None
    payload: str = "{}"
    content_hash: bytes = b"\x11" * 32


@dataclass(frozen=True, slots=True)
class WorkerCall:
    database_url: str
    organization_id: UUID
    actor_user_id: UUID
    append: DirectAppend
    backend_pids: Queue[int]


def _runtime_database_url() -> str:
    database_name = str(connection.settings_dict["NAME"])
    return database_url_for_name(os.environ["APP_DATABASE_URL"], database_name)


@pytest.mark.parametrize(
    ("database_name", "expected_path"),
    [
        pytest.param("clinic?url", "/clinic%3Furl", id="question"),
        pytest.param("clinic#url", "/clinic%23url", id="hash"),
        pytest.param("clinic/url", "/clinic%2Furl", id="slash"),
        pytest.param("clinic%url", "/clinic%25url", id="percent"),
        pytest.param("clinic url", "/clinic%20url", id="space"),
        pytest.param("clinic%3Furl", "/clinic%253Furl", id="literal-escape"),
        pytest.param("clinic_url", "/clinic_url", id="ordinary"),
    ],
)
def test_runtime_database_url_encodes_active_name_as_one_path_segment(
    monkeypatch: pytest.MonkeyPatch,
    database_name: str,
    expected_path: str,
) -> None:
    configured_url = urlsplit(
        "postgresql://user:p%40ss@db.example:5544/base"
        "?application_name=harness%20qa&connect_timeout=5#marker"
    )
    monkeypatch.setenv("APP_DATABASE_URL", urlunsplit(configured_url))
    monkeypatch.setitem(connection.settings_dict, "NAME", database_name)

    result = urlsplit(_runtime_database_url())

    assert result.path == expected_path
    assert (result.scheme, result.netloc, result.query, result.fragment) == (
        configured_url.scheme,
        configured_url.netloc,
        configured_url.query,
        configured_url.fragment,
    )


def _set_context(
    raw_connection: psycopg.Connection[TupleRow],
    organization_id: UUID,
    actor_user_id: UUID,
) -> None:
    raw_connection.execute(
        "SELECT set_config('app.current_tenant', %s, true), "
        "set_config('app.current_user_id', %s, true)",
        (str(organization_id), str(actor_user_id)),
    )


def _direct_append(
    raw_connection: psycopg.Connection[TupleRow], append: DirectAppend
) -> int:
    row = raw_connection.execute(
        "SELECT clinic_app.audit_append("
        "%s::text, %s::text, %s::inet, %s::text, %s::text, "
        "%s::timestamptz, %s::jsonb, %s::bytea)",
        (
            append.event_type,
            append.component_id,
            append.component_ip,
            append.affected_record_type,
            append.affected_record_id,
            append.occurred_at_utc or datetime.now(UTC),
            append.payload,
            append.content_hash,
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _append_worker(call: WorkerCall) -> int:
    with psycopg.connect(call.database_url) as raw_connection:
        raw_connection.execute("SET LOCAL statement_timeout = '2s'")
        _set_context(raw_connection, call.organization_id, call.actor_user_id)
        call.backend_pids.put(raw_connection.info.backend_pid)
        return _direct_append(raw_connection, call.append)


def _wait_for_advisory_waiter(backend_pid: int) -> None:
    deadline = monotonic() + 3
    with connection.cursor() as cursor:
        while monotonic() < deadline:
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks "
                "WHERE pid = %s AND locktype = 'advisory' AND NOT granted)",
                [backend_pid],
            )
            if cursor.fetchone() == (True,):
                return
    pytest.fail("append worker did not reach the advisory-lock wait state")


def _expected_content_hash(
    event: AuditEventInput,
    context: AuditTrustedContext,
    payload: Mapping[str, str | int],
) -> bytes:
    content = {
        "v": "clinic-audit-v1",
        "organization_id": str(context.organization_id),
        "event_type": event.event_type,
        "component": {
            "identifier": event.component_id,
            "ip": None if event.component_ip is None else str(event.component_ip),
        },
        "actor": {
            "user_id": (
                None if context.actor_user_id is None else str(context.actor_user_id)
            )
        },
        "affected_record_type": event.affected_record_type,
        "affected_record_id": event.affected_record_id,
        "occurred_at_utc": event.occurred_at_utc.astimezone(UTC).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        ),
        "payload": dict(payload),
    }
    return sha256(b"clinic-audit-v1\x00" + rfc8785.dumps(content)).digest()


def test_audit_event_is_registered_with_exact_model_shape() -> None:
    # Given: the installed audit Django application
    # When: its ledger model is resolved from the registry
    audit_event = django_apps.get_model("audit", "AuditEvent")
    fields = {field.name: field for field in audit_event._meta.fields}

    # Then: the state model exposes only the approved ledger columns
    assert audit_event._meta.db_table == "audit_event"
    assert tuple(fields) == (
        "seq",
        "organization_id",
        "actor_user_id",
        "event_type",
        "component_id",
        "component_ip",
        "affected_record_type",
        "affected_record_id",
        "occurred_at_utc",
        "payload",
        "prev_hash",
        "curr_hash",
    )
    assert fields["seq"].primary_key is True
    assert fields["curr_hash"].unique is True
    assert "content_hash" not in fields


def test_rfc8785_appendix_vector_is_exact() -> None:
    # Given: the RFC 8785 Appendix B-style mixed JSON vector
    value: CanonicalValue = {
        "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 1e-27],
        "string": '€$\u000f\nA\'B"\\"/',
    }

    # When: the dependency canonicalizes the vector
    canonical = rfc8785.dumps(value)

    # Then: ECMAScript number and string serialization is byte-exact
    assert canonical == (
        b'{"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
        b'"string":"\xe2\x82\xac$\\u000f\\nA\'B\\"\\\\\\"/"}'
    )


def test_service_rejects_unknown_payload_key_before_sql() -> None:
    # Given: the service module and an event without any trusted DB context
    assert importlib.util.find_spec("apps.audit.services") is not None
    event = AuditEventInput(
        event_type="audit.synthetic",
        component_id="test-suite",
        component_ip=ip_address("127.0.0.1"),
        affected_record_type=None,
        affected_record_id=None,
        occurred_at_utc=datetime.now(UTC),
    )
    assert {
        "http_method",
        "http_status",
        "object_verb",
        "reason_code",
        "request_id",
    } == AUDIT_PAYLOAD_ALLOWED

    # When / Then: the typed boundary rejects the synthetic forbidden key first
    with pytest.raises(AuditPayloadKeyRejected, match="cpf") as exc_info:
        record_event(event, payload={"cpf": None})
    assert exc_info.value.key == "cpf"


@pytest.mark.parametrize(
    ("payload", "expected_key"),
    [
        ({"http_status": True}, "http_status"),
        ({"http_status": 99}, "http_status"),
        ({"http_status": 600}, "http_status"),
        ({"reason_code": ""}, "reason_code"),
        ({"reason_code": 1.5}, "reason_code"),
        ({"reason_code": Decimal(1)}, "reason_code"),
        ({"reason_code": ["nested"]}, "reason_code"),
        ({"reason_code": {"nested": "value"}}, "reason_code"),
        ({"request_id": None}, "request_id"),
    ],
)
def test_service_rejects_noncanonical_payload_values(
    payload: dict[
        str,
        bool | int | float | str | Decimal | list[str] | dict[str, str] | None,
    ],
    expected_key: str,
) -> None:
    # Given: a valid semantic event and one noncanonical payload value
    event = AuditEventInput(
        event_type="audit.synthetic",
        component_id="test-suite",
        component_ip=None,
        affected_record_type=None,
        affected_record_id=None,
        occurred_at_utc=datetime.now(UTC),
    )

    # When / Then: floats, Decimal, booleans, nulls, and nesting are rejected
    with pytest.raises(AuditPayloadValueRejectedError) as exc_info:
        record_event(event, payload=payload)
    assert exc_info.value.key == expected_key


def test_canonical_hash_is_order_invariant_without_unicode_normalization() -> None:
    # Given: semantically equal payload orders and canonically distinct Unicode
    organization_id = uuid4()
    actor_user_id = uuid4()
    occurred_at_utc = datetime.now(UTC).replace(microsecond=123456)
    event = AuditEventInput(
        event_type="audit.synthetic",
        component_id="test-suite",
        component_ip=ip_address("2001:db8::1"),
        affected_record_type="synthetic_record",
        affected_record_id="record-1",
        occurred_at_utc=occurred_at_utc,
    )
    ordered_a = {"object_verb": "created", "reason_code": "é"}
    ordered_b = {"reason_code": "é", "object_verb": "created"}
    decomposed = {"object_verb": "created", "reason_code": "e\u0301"}

    # When: hashes are computed from the typed semantic content
    context = AuditTrustedContext(organization_id, actor_user_id)
    hash_a = _content_hash(event, context, ordered_a)
    hash_b = _content_hash(event, context, ordered_b)
    hash_decomposed = _content_hash(
        event,
        context,
        decomposed,
    )

    # Then: key order is irrelevant while Unicode code points remain distinct
    assert hash_a == hash_b
    assert hash_a != hash_decomposed


@pytest.mark.django_db(transaction=True)
def test_catalog_has_exact_audit_schema_acl_and_functions() -> None:
    # Given: the migrated PostgreSQL catalog
    with connection.cursor() as cursor:
        # When: relation, identity, extension, constraint, and index facts are read
        cursor.execute(
            """
            SELECT c.relkind, pg_get_userbyid(c.relowner), a.attidentity
            FROM pg_class AS c
            JOIN pg_attribute AS a ON a.attrelid = c.oid AND a.attname = 'seq'
            WHERE c.oid = 'clinic_app.audit_event'::regclass
            """
        )
        table_fact = cursor.fetchone()
        cursor.execute(
            """
            SELECT column_name, udt_name, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'clinic_app' AND table_name = 'audit_event'
            ORDER BY ordinal_position
            """
        )
        columns = cursor.fetchall()
        cursor.execute(
            """
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'clinic_app.audit_event'::regclass
            ORDER BY conname
            """
        )
        constraints = {row[0] for row in cursor.fetchall()}
        cursor.execute(
            """
            SELECT pg_get_indexdef(indexrelid)
            FROM pg_index
            WHERE indrelid = 'clinic_app.audit_event'::regclass
            """
        )
        indexes = "\n".join(row[0] for row in cursor.fetchall())
        cursor.execute(
            """
            SELECT n.nspname, e.extversion
            FROM pg_extension AS e JOIN pg_namespace AS n ON n.oid = e.extnamespace
            WHERE e.extname = 'pgcrypto'
            """
        )
        extension = cursor.fetchone()
        cursor.execute(
            """
            SELECT p.proname, pg_get_function_identity_arguments(p.oid),
                   p.proargnames, pg_get_userbyid(p.proowner),
                   p.prorettype::regtype::text,
                   p.prosecdef, p.provolatile, p.proparallel, p.proconfig,
                   has_function_privilege('clinic_app', p.oid, 'EXECUTE'),
                   has_function_privilege('clinic_resolver', p.oid, 'EXECUTE'),
                   EXISTS (
                       SELECT 1
                       FROM aclexplode(
                           COALESCE(p.proacl, acldefault('f', p.proowner))
                       ) AS privilege
                       WHERE privilege.grantee = 0
                         AND privilege.privilege_type = 'EXECUTE'
                   )
            FROM pg_proc AS p
            WHERE p.pronamespace = 'clinic_app'::regnamespace
              AND p.proname IN ('audit_append', 'audit_append_system')
            ORDER BY p.proname
            """
        )
        functions = cursor.fetchall()
        cursor.execute(
            "SELECT to_regprocedure(%s) IS NOT NULL, to_regprocedure(%s) IS NOT NULL",
            [APPEND_SIGNATURE, SYSTEM_APPEND_SIGNATURE],
        )
        resolved_signatures = cursor.fetchone()
        cursor.execute(
            """
            SELECT pg_get_userbyid(c.relowner), c.reloptions, pg_get_viewdef(c.oid),
                   has_table_privilege('clinic_app', c.oid, 'SELECT'),
                   has_table_privilege(
                       'clinic_app', c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE'
                   )
            FROM pg_class AS c
            WHERE c.oid = 'clinic_app.audit_event_tenant'::regclass
            """
        )
        view_fact = cursor.fetchone()
        cursor.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'clinic_app'
              AND table_name = 'audit_event_tenant'
            ORDER BY ordinal_position
            """
        )
        view_columns = [row[0] for row in cursor.fetchall()]

    # Then: the catalog proves the exact hardened posture
    assert table_fact == ("r", "clinic_owner", "a")
    assert tuple(name for name, _, _ in columns) == (
        "seq",
        "organization_id",
        "actor_user_id",
        "event_type",
        "component_id",
        "component_ip",
        "affected_record_type",
        "affected_record_id",
        "occurred_at_utc",
        "payload",
        "prev_hash",
        "curr_hash",
    )
    assert {name: data_type for name, data_type, _ in columns} == {
        "seq": "int8",
        "organization_id": "uuid",
        "actor_user_id": "uuid",
        "event_type": "varchar",
        "component_id": "varchar",
        "component_ip": "inet",
        "affected_record_type": "varchar",
        "affected_record_id": "varchar",
        "occurred_at_utc": "timestamptz",
        "payload": "jsonb",
        "prev_hash": "bytea",
        "curr_hash": "bytea",
    }
    assert {name for name, _, nullable in columns if nullable == "YES"} == {
        "actor_user_id",
        "component_ip",
        "affected_record_type",
        "affected_record_id",
    }
    assert {
        "audit_event_actor_scope_ck",
        "audit_event_affected_pair_ck",
        "audit_event_affected_text_ck",
        "audit_event_component_id_text_ck",
        "audit_event_component_ip_host_ck",
        "audit_event_curr_hash_len_ck",
        "audit_event_event_type_text_ck",
        "audit_event_payload_object_ck",
        "audit_event_prev_hash_len_ck",
        "audit_event_timestamp_finite_ck",
    }.issubset(constraints)
    assert "(organization_id, seq DESC) INCLUDE (curr_hash)" in indexes
    assert "UNIQUE" in indexes
    assert "curr_hash" in indexes
    assert extension == ("clinic_app", "1.3")
    assert resolved_signatures == (True, True)
    assert functions == [
        (
            "audit_append",
            "event_type text, component_id text, component_ip inet, "
            "affected_record_type text, affected_record_id text, "
            "occurred_at_utc timestamp with time zone, payload jsonb, "
            "content_hash bytea",
            [
                "event_type",
                "component_id",
                "component_ip",
                "affected_record_type",
                "affected_record_id",
                "occurred_at_utc",
                "payload",
                "content_hash",
            ],
            "clinic_owner",
            "bigint",
            True,
            "v",
            "u",
            ["search_path=pg_catalog, pg_temp"],
            True,
            False,
            False,
        ),
        (
            "audit_append_system",
            "event_type text, component_id text, component_ip inet, "
            "affected_record_type text, affected_record_id text, "
            "occurred_at_utc timestamp with time zone, payload jsonb, "
            "content_hash bytea",
            [
                "event_type",
                "component_id",
                "component_ip",
                "affected_record_type",
                "affected_record_id",
                "occurred_at_utc",
                "payload",
                "content_hash",
            ],
            "clinic_owner",
            "bigint",
            True,
            "v",
            "u",
            ["search_path=pg_catalog, pg_temp"],
            False,
            False,
            False,
        ),
    ]
    assert view_fact is not None
    assert view_fact[0] == "clinic_owner"
    assert set(view_fact[1]) == {"security_barrier=true", "security_invoker=false"}
    assert "00000000-0000-0000-0000-000000000000" in view_fact[2]
    assert "current_setting('app.current_tenant'::text, true)" in view_fact[2]
    assert view_fact[3:] == (True, False)
    assert view_columns == [
        "seq",
        "organization_id",
        "actor_user_id",
        "event_type",
        "component_id",
        "component_ip",
        "affected_record_type",
        "affected_record_id",
        "occurred_at_utc",
        "payload",
        "prev_hash",
        "curr_hash",
    ]


@pytest.mark.django_db(transaction=True)
def test_relation_acls_deny_base_and_sequence_but_allow_tenant_view_select() -> None:
    # Given: all non-owner database principals
    with connection.cursor() as cursor:
        # When: effective relation privileges are inspected
        cursor.execute(
            """
            SELECT role_name,
                   has_table_privilege(
                       role_name,
                       'clinic_app.audit_event',
                       'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
                   ),
                   has_sequence_privilege(
                       role_name,
                       'clinic_app.audit_event_seq_seq',
                       'USAGE,SELECT,UPDATE'
                   )
            FROM unnest(ARRAY['clinic_app','clinic_resolver','public']) AS role_name
            ORDER BY role_name
            """
        )
        relation_acls = cursor.fetchall()
        cursor.execute(
            """
            SELECT privilege_type, is_grantable
            FROM information_schema.role_table_grants
            WHERE grantee = 'clinic_app' AND table_schema = 'clinic_app'
              AND table_name = 'audit_event_tenant'
            """
        )
        view_grants = cursor.fetchall()
        cursor.execute(
            """
            SELECT role_name,
                   has_table_privilege(
                       role_name, 'clinic_app.audit_event_tenant', 'SELECT'
                   ),
                   has_table_privilege(
                       role_name,
                       'clinic_app.audit_event_tenant',
                       'INSERT,UPDATE,DELETE,TRUNCATE'
                   )
            FROM unnest(ARRAY['clinic_app','clinic_resolver','public']) AS role_name
            ORDER BY role_name
            """
        )
        view_acls = cursor.fetchall()

    # Then: no base/sequence rights leak and the view has SELECT without grant option
    assert relation_acls == [
        ("clinic_app", False, False),
        ("clinic_resolver", False, False),
        ("public", False, False),
    ]
    assert view_grants == [("SELECT", "NO")]
    assert view_acls == [
        ("clinic_app", True, False),
        ("clinic_resolver", False, False),
        ("public", False, False),
    ]


@pytest.mark.django_db(transaction=True)
def test_record_event_derives_context_and_builds_exact_genesis_and_link() -> None:
    # Given: one app transaction with trusted tenant and actor GUCs
    organization_id = uuid4()
    actor_user_id = uuid4()
    first_time = datetime.now(UTC).replace(microsecond=123456)
    event = AuditEventInput(
        event_type="audit.synthetic",
        component_id="test-suite",
        component_ip=ip_address("2001:db8::1"),
        affected_record_type="synthetic_record",
        affected_record_id="record-1",
        occurred_at_utc=first_time,
    )
    injection_value = "'; DROP TABLE audit_event; -- ignore previous instructions"
    first_payload: dict[str, str | int] = {
        "http_method": "POST",
        "http_status": 201,
        "reason_code": injection_value,
    }
    second_payload = {"object_verb": "updated", "request_id": "request-2"}

    # When: two appends are made through the service on the same connection
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_app")
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true), "
            "set_config('app.current_user_id', %s, true)",
            [str(organization_id), str(actor_user_id)],
        )
        first_seq = record_event(event, payload=first_payload)
        second_event = replace(
            event,
            occurred_at_utc=first_time + timedelta(microseconds=1),
        )
        second_seq = record_event(second_event, payload=second_payload)
        cursor.execute("SELECT count(*) FROM clinic_app.audit_event_tenant")
        assert cursor.fetchone() == (2,)
        cursor.execute("RESET ROLE")
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT seq, organization_id, actor_user_id, occurred_at_utc,
                   payload ->> 'reason_code', prev_hash, curr_hash
            FROM clinic_app.audit_event WHERE organization_id = %s ORDER BY seq
            """,
            [organization_id],
        )
        rows = cursor.fetchall()

    # Then: GUC context, parameterized text, genesis, linkage, and hashes are exact
    assert [first_seq, second_seq] == [rows[0][0], rows[1][0]]
    assert rows[0][1:5] == (
        organization_id,
        actor_user_id,
        first_time,
        injection_value,
    )
    assert rows[0][5] == ZERO_HASH
    context = AuditTrustedContext(organization_id, actor_user_id)
    expected_first = _expected_content_hash(event, context, first_payload)
    assert rows[0][6] == sha256(expected_first + ZERO_HASH).digest()
    assert rows[1][5] == rows[0][6]
    expected_second = _expected_content_hash(second_event, context, second_payload)
    assert rows[1][6] == sha256(expected_second + rows[0][6]).digest()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "operation",
    ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"],
)
def test_app_cannot_access_base_table(operation: str) -> None:
    # Given: a direct clinic_app connection
    statements = {
        "SELECT": "SELECT * FROM clinic_app.audit_event",
        "INSERT": "INSERT INTO clinic_app.audit_event DEFAULT VALUES",
        "UPDATE": "UPDATE clinic_app.audit_event SET event_type = 'changed'",
        "DELETE": "DELETE FROM clinic_app.audit_event",
        "TRUNCATE": "TRUNCATE clinic_app.audit_event",
    }

    # When / Then: every raw base-table operation is permission denied
    with (
        psycopg.connect(_runtime_database_url()) as raw_connection,
        pytest.raises(InsufficientPrivilege),
    ):
        raw_connection.execute(statements[operation])


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("tenant_value", [None, "", "malformed", str(SYSTEM_ORG_ID)])
def test_tenant_append_rejects_untrusted_tenant_context(
    tenant_value: str | None,
) -> None:
    # Given: an app connection with an actor but invalid or absent tenant context
    with psycopg.connect(_runtime_database_url()) as raw_connection:
        raw_connection.execute(
            "SELECT set_config('app.current_user_id', %s, true)", [str(uuid4())]
        )
        if tenant_value is not None:
            raw_connection.execute(
                "SELECT set_config('app.current_tenant', %s, true)", [tenant_value]
            )

        # When / Then: trusted-context validation rejects before insert
        with pytest.raises(InvalidParameterValue):
            _direct_append(raw_connection, DirectAppend())


@pytest.mark.django_db(transaction=True)
def test_tenant_append_rejects_missing_actor_and_non_read_committed_isolation() -> None:
    # Given: separate app transactions missing an actor or using serializable isolation
    database_url = _runtime_database_url()

    # When / Then: each nonconforming context is rejected
    with psycopg.connect(database_url) as raw_connection:
        raw_connection.execute(
            "SELECT set_config('app.current_tenant', %s, true)", [str(uuid4())]
        )
        with pytest.raises(InvalidParameterValue):
            _direct_append(raw_connection, DirectAppend())
    with psycopg.connect(database_url) as raw_connection:
        raw_connection.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        _set_context(raw_connection, uuid4(), uuid4())
        with pytest.raises(InvalidParameterValue):
            _direct_append(raw_connection, DirectAppend())


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("actor_value", [None, "", "malformed"])
def test_tenant_append_rejects_invalid_actor_context(
    actor_value: str | None,
) -> None:
    # Given: a valid tenant with an absent, empty, or malformed actor setting
    with psycopg.connect(_runtime_database_url()) as raw_connection:
        raw_connection.execute(
            "SELECT set_config('app.current_tenant', %s, true)", [str(uuid4())]
        )
        if actor_value is not None:
            raw_connection.execute(
                "SELECT set_config('app.current_user_id', %s, true)", [actor_value]
            )

        # When / Then: trusted actor validation rejects before insert
        with pytest.raises(InvalidParameterValue):
            _direct_append(raw_connection, DirectAppend())


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "append",
    [
        DirectAppend(content_hash=b"short"),
        DirectAppend(event_type=""),
        DirectAppend(component_id="   "),
        DirectAppend(payload='{"cpf":null}'),
        DirectAppend(payload='{"reason_code":1.5}'),
        DirectAppend(occurred_at_utc=datetime(2000, 1, 1, tzinfo=UTC)),
        DirectAppend(component_ip="192.0.2.1/24"),
        DirectAppend(affected_record_type="synthetic_record", affected_record_id=None),
    ],
)
def test_database_append_revalidates_malformed_arguments(append: DirectAppend) -> None:
    # Given: a valid trusted context with one malformed direct-SQL argument
    with psycopg.connect(_runtime_database_url()) as raw_connection:
        _set_context(raw_connection, uuid4(), uuid4())

        # When / Then: the definer boundary independently rejects the argument
        with pytest.raises(InvalidParameterValue):
            _direct_append(raw_connection, append)


@pytest.mark.django_db(transaction=True)
def test_system_append_is_owner_only_and_never_visible_in_tenant_view() -> None:
    # Given: the owner-only Python system path and a synthetic system event
    event = AuditEventInput(
        event_type="audit.synthetic",
        component_id="test-suite",
        component_ip=None,
        affected_record_type=None,
        affected_record_id=None,
        occurred_at_utc=datetime.now(UTC),
    )

    # When: owner appends with absent and present optional actor contexts
    first_payload = {"reason_code": "system-test"}
    first_seq = _record_system_event(event, payload=first_payload)
    actor_user_id = uuid4()
    second_event = replace(
        event,
        occurred_at_utc=event.occurred_at_utc + timedelta(microseconds=1),
    )
    second_payload = {"reason_code": "system-actor-test"}
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_user_id', %s, true)",
            [str(actor_user_id)],
        )
        second_seq = _record_system_event(second_event, payload=second_payload)
    with (
        psycopg.connect(_runtime_database_url()) as raw_connection,
        pytest.raises(InsufficientPrivilege),
    ):
        raw_connection.execute(
            "SELECT clinic_app.audit_append_system(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                "audit.synthetic",
                "test-suite",
                None,
                None,
                None,
                datetime.now(UTC),
                "{}",
                b"\x22" * 32,
            ),
        )
    with psycopg.connect(_runtime_database_url()) as raw_connection:
        raw_connection.execute(
            "SELECT set_config('app.current_tenant', %s, true)", [str(SYSTEM_ORG_ID)]
        )
        visible = raw_connection.execute(
            "SELECT count(*) FROM clinic_app.audit_event_tenant"
        ).fetchone()

    # Then: system hashes and optional actor context are exact and view-hidden
    assert visible == (0,)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT seq, organization_id, actor_user_id, occurred_at_utc, "
            "prev_hash, curr_hash FROM clinic_app.audit_event "
            "WHERE seq IN (%s, %s) ORDER BY seq",
            [first_seq, second_seq],
        )
        rows = cursor.fetchall()
    assert rows[0][1:5] == (SYSTEM_ORG_ID, None, event.occurred_at_utc, ZERO_HASH)
    first_content_hash = _expected_content_hash(
        event,
        AuditTrustedContext(SYSTEM_ORG_ID, None),
        first_payload,
    )
    assert rows[0][5] == sha256(first_content_hash + ZERO_HASH).digest()
    assert rows[1][1:5] == (
        SYSTEM_ORG_ID,
        actor_user_id,
        second_event.occurred_at_utc,
        rows[0][5],
    )
    second_content_hash = _expected_content_hash(
        second_event,
        AuditTrustedContext(SYSTEM_ORG_ID, actor_user_id),
        second_payload,
    )
    assert rows[1][5] == sha256(second_content_hash + rows[0][5]).digest()


@pytest.mark.django_db(transaction=True)
def test_tenant_view_exposes_only_current_organization() -> None:
    # Given: committed rows in two independent tenant chains
    database_url = _runtime_database_url()
    organization_a, organization_b = uuid4(), uuid4()
    for hash_byte, organization_id in enumerate(
        (organization_a, organization_b),
        start=61,
    ):
        with psycopg.connect(database_url) as raw_connection:
            _set_context(raw_connection, organization_id, uuid4())
            _direct_append(
                raw_connection,
                DirectAppend(content_hash=bytes([hash_byte]) * 32),
            )

    # When: organization A selects only through the tenant view
    with psycopg.connect(database_url) as raw_connection:
        _set_context(raw_connection, organization_a, uuid4())
        visible = raw_connection.execute(
            "SELECT organization_id FROM clinic_app.audit_event_tenant"
        ).fetchall()

    # Then: no organization B row crosses the view boundary
    assert visible == [(organization_a,)]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("tenant_value", [None, "", "malformed", str(SYSTEM_ORG_ID)])
def test_tenant_view_fails_closed_for_invalid_context(
    tenant_value: str | None,
) -> None:
    # Given: a committed tenant row and an invalid tenant view context
    database_url = _runtime_database_url()
    with psycopg.connect(database_url) as seed_connection:
        _set_context(seed_connection, uuid4(), uuid4())
        _direct_append(seed_connection, DirectAppend())
    with psycopg.connect(database_url) as raw_connection:
        if tenant_value is not None:
            raw_connection.execute(
                "SELECT set_config('app.current_tenant', %s, true)", [tenant_value]
            )

        # When: the app selects the tenant view
        visible = raw_connection.execute(
            "SELECT count(*) FROM clinic_app.audit_event_tenant"
        ).fetchone()

    # Then: unset, empty, malformed, and sentinel contexts expose no rows
    assert visible == (0,)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("repetition", range(3))
def test_same_chain_serializes_and_links_without_sleep(repetition: int) -> None:
    # Given: A holds one tenant chain lock after its append and B is synchronized
    database_url = _runtime_database_url()
    organization_id, actor_a, actor_b = uuid4(), uuid4(), uuid4()
    backend_pids: Queue[int] = Queue()
    with psycopg.connect(database_url) as connection_a:
        _set_context(connection_a, organization_id, actor_a)
        seq_a = _direct_append(
            connection_a, DirectAppend(content_hash=bytes([repetition + 1]) * 32)
        )
        call = WorkerCall(
            database_url,
            organization_id,
            actor_b,
            DirectAppend(content_hash=bytes([repetition + 11]) * 32),
            backend_pids,
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            future_b = executor.submit(_append_worker, call)
            _wait_for_advisory_waiter(backend_pids.get(timeout=2))

            # When: A commits and releases the transaction-scoped advisory lock
            connection_a.commit()
            seq_b = future_b.result(timeout=3)

    # Then: B commits after A and its predecessor is A's current hash
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT seq, prev_hash, curr_hash FROM clinic_app.audit_event "
            "WHERE organization_id = %s ORDER BY seq",
            [organization_id],
        )
        rows = cursor.fetchall()
    assert [row[0] for row in rows] == [seq_a, seq_b]
    assert rows[1][1] == rows[0][2]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("repetition", range(3))
def test_different_chains_complete_before_held_chain_commits(repetition: int) -> None:
    # Given: A holds org A's advisory lock while B targets a different organization
    database_url = _runtime_database_url()
    backend_pids: Queue[int] = Queue()
    with psycopg.connect(database_url) as connection_a:
        _set_context(connection_a, uuid4(), uuid4())
        _direct_append(
            connection_a, DirectAppend(content_hash=bytes([repetition + 21]) * 32)
        )
        call = WorkerCall(
            database_url,
            uuid4(),
            uuid4(),
            DirectAppend(content_hash=bytes([repetition + 31]) * 32),
            backend_pids,
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            # When: B appends under a bounded timeout before A commits
            future_b = executor.submit(_append_worker, call)
            backend_pids.get(timeout=2)
            seq_b = future_b.result(timeout=2)
            assert connection_a.info.transaction_status.name == "INTRANS"

    # Then: the independent append completed successfully
    assert seq_b > 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("repetition", range(3))
def test_rolled_back_holder_releases_lock_and_next_append_is_genesis(
    repetition: int,
) -> None:
    # Given: an append whose transaction will be interrupted by rollback
    database_url = _runtime_database_url()
    organization_id = uuid4()
    with psycopg.connect(database_url) as aborted_connection:
        _set_context(aborted_connection, organization_id, uuid4())
        aborted_seq = _direct_append(
            aborted_connection,
            DirectAppend(content_hash=bytes([repetition + 41]) * 32),
        )
        aborted_connection.rollback()

        # When: the same chain appends immediately after the holder rollback
        _set_context(aborted_connection, organization_id, uuid4())
        committed_seq = _direct_append(
            aborted_connection,
            DirectAppend(content_hash=bytes([repetition + 51]) * 32),
        )

    # Then: sequence gaps are allowed, the lock is released, and genesis is correct
    assert committed_seq > aborted_seq
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT seq, prev_hash FROM clinic_app.audit_event "
            "WHERE organization_id = %s",
            [organization_id],
        )
        assert cursor.fetchone() == (committed_seq, ZERO_HASH)
