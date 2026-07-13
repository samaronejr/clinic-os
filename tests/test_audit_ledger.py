"""PostgreSQL audit immutability and chain-verification specification.

# noqa: SIZE_OK
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from ipaddress import ip_address
from typing import TYPE_CHECKING, Final, assert_never
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.audit import services as audit_services
from apps.audit.models import SYSTEM_ORG_ID
from apps.audit.services import AuditEventInput, _record_system_event, record_event
from django.db import connection, connections, transaction
from django.db.backends.postgresql.base import DatabaseWrapper
from django.db.utils import DatabaseError
from psycopg.errors import DivisionByZero, InsufficientPrivilege

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.db.backends.utils import CursorWrapper

ZERO_HASH: Final = b"\x00" * 32
ROW_TRIGGER: Final = "audit_event_immutable_row"
TRUNCATE_TRIGGER: Final = "audit_event_immutable_truncate"
TRIGGER_FUNCTION: Final = "audit_event_reject_mutation"
pytestmark = pytest.mark.usefixtures("audit_triggers_finally_enabled")


@dataclass(frozen=True, slots=True)
class PinnedChain:
    organization_id: UUID
    actor_user_id: UUID
    first_seq: int
    second_seq: int
    first_curr_hash: bytes
    second_prev_hash: bytes
    second_curr_hash: bytes


class TamperCase(StrEnum):
    CONTENT = "content"
    LINKAGE = "linkage"
    GENESIS = "genesis"
    SECOND_GENESIS = "second_genesis"


class SemanticTamperCase(StrEnum):
    FORBIDDEN_PAYLOAD = "forbidden_payload"
    TIMESTAMP = "timestamp"
    INSTRUCTION_PAYLOAD = "instruction_payload"


def _event(occurred_at_utc: datetime) -> AuditEventInput:
    return AuditEventInput(
        event_type="audit.synthetic",
        component_id="ledger-test",
        component_ip=ip_address("192.0.2.40"),
        affected_record_type="synthetic_record",
        affected_record_id="record-1",
        occurred_at_utc=occurred_at_utc,
    )


def _append_tenant_chain() -> PinnedChain:
    organization_id = uuid4()
    actor_user_id = uuid4()
    first_event = _event(datetime.now(UTC).replace(microsecond=111111))
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_app")
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true), "
            "set_config('app.current_user_id', %s, true)",
            [str(organization_id), str(actor_user_id)],
        )
        first_seq = record_event(
            first_event,
            payload={"object_verb": "created", "request_id": "ledger-1"},
        )
        second_seq = record_event(
            replace(
                first_event,
                occurred_at_utc=first_event.occurred_at_utc + timedelta(microseconds=1),
            ),
            payload={"object_verb": "updated", "request_id": "ledger-2"},
        )
        cursor.execute("RESET ROLE")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT seq, prev_hash, curr_hash FROM clinic_app.audit_event "
            "WHERE organization_id = %s ORDER BY seq",
            [organization_id],
        )
        rows = cursor.fetchall()
    assert len(rows) == 2
    assert rows[0][0] == first_seq
    assert rows[1][0] == second_seq
    return PinnedChain(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        first_seq=first_seq,
        second_seq=second_seq,
        first_curr_hash=bytes(rows[0][2]),
        second_prev_hash=bytes(rows[1][1]),
        second_curr_hash=bytes(rows[1][2]),
    )


def _set_tenant_context(
    database_connection: DatabaseWrapper,
    organization_id: UUID,
    actor_user_id: UUID,
) -> None:
    with database_connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true), "
            "set_config('app.current_user_id', %s, true)",
            [str(organization_id), str(actor_user_id)],
        )


def _disable_immutability(cursor: CursorWrapper) -> None:
    cursor.execute(f"ALTER TABLE clinic_app.audit_event DISABLE TRIGGER {ROW_TRIGGER}")
    cursor.execute(
        f"ALTER TABLE clinic_app.audit_event DISABLE TRIGGER {TRUNCATE_TRIGGER}"
    )


def _enable_immutability(cursor: CursorWrapper) -> None:
    cursor.execute(f"ALTER TABLE clinic_app.audit_event ENABLE TRIGGER {ROW_TRIGGER}")
    cursor.execute(
        f"ALTER TABLE clinic_app.audit_event ENABLE TRIGGER {TRUNCATE_TRIGGER}"
    )


def _assert_triggers_enabled() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname, tgenabled FROM pg_trigger "
            "WHERE tgrelid = 'clinic_app.audit_event'::regclass "
            "AND tgname IN (%s, %s) ORDER BY tgname",
            [ROW_TRIGGER, TRUNCATE_TRIGGER],
        )
        assert cursor.fetchall() == [
            (ROW_TRIGGER, "O"),
            (TRUNCATE_TRIGGER, "O"),
        ]


@pytest.fixture
def audit_superuser_connection(
    superuser_database_url: str,
) -> Iterator[DatabaseWrapper]:
    connection_info = psycopg.conninfo.conninfo_to_dict(superuser_database_url)
    settings = connections["default"].settings_dict.copy()
    settings.update(
        {
            "NAME": connection_info["dbname"],
            "USER": connection_info["user"],
            "PASSWORD": connection_info["password"],
            "HOST": connection_info["host"],
            "PORT": connection_info["port"],
        }
    )
    wrapper = DatabaseWrapper(settings, alias="audit_superuser")
    try:
        yield wrapper
    finally:
        wrapper.close()


@pytest.mark.django_db(transaction=True)
def test_catalog_has_exact_immutable_triggers_function_and_acls() -> None:
    # Given: the fully migrated audit ledger
    with connection.cursor() as cursor:
        # When: trigger shape, function posture, and mutation ACLs are read
        cursor.execute(
            """
            SELECT trigger.tgname, trigger.tgenabled,
                   (trigger.tgtype & 1) = 1 AS row_level,
                   (trigger.tgtype & 2) = 2 AS before_timing,
                   (trigger.tgtype & 8) = 8 AS fires_delete,
                   (trigger.tgtype & 16) = 16 AS fires_update,
                   (trigger.tgtype & 32) = 32 AS fires_truncate,
                   procedure.proname, pg_get_userbyid(procedure.proowner)
            FROM pg_trigger AS trigger
            JOIN pg_proc AS procedure ON procedure.oid = trigger.tgfoid
            WHERE trigger.tgrelid = 'clinic_app.audit_event'::regclass
              AND NOT trigger.tgisinternal
            ORDER BY trigger.tgname
            """
        )
        triggers = cursor.fetchall()
        cursor.execute(
            """
            SELECT pg_get_userbyid(proowner), prosecdef,
                   has_function_privilege('public', oid, 'EXECUTE'),
                   has_function_privilege('clinic_app', oid, 'EXECUTE'),
                   has_function_privilege('clinic_resolver', oid, 'EXECUTE')
            FROM pg_proc
            WHERE oid = 'clinic_app.audit_event_reject_mutation()'::regprocedure
            """
        )
        function_posture = cursor.fetchone()
        cursor.execute(
            """
            SELECT role_name,
                   has_table_privilege(
                       role_name, 'clinic_app.audit_event',
                       'INSERT,UPDATE,DELETE,TRUNCATE'
                   )
            FROM unnest(ARRAY['clinic_app','clinic_resolver','public']) role_name
            ORDER BY role_name
            """
        )
        mutation_acls = cursor.fetchall()

    # Then: both enabled triggers share one owner function and no app DML leaks
    assert triggers == [
        (
            ROW_TRIGGER,
            "O",
            True,
            True,
            True,
            True,
            False,
            TRIGGER_FUNCTION,
            "clinic_owner",
        ),
        (
            TRUNCATE_TRIGGER,
            "O",
            False,
            True,
            False,
            False,
            True,
            TRIGGER_FUNCTION,
            "clinic_owner",
        ),
    ]
    assert function_posture == ("clinic_owner", False, False, False, False)
    assert mutation_acls == [
        ("clinic_app", False),
        ("clinic_resolver", False),
        ("public", False),
    ]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE clinic_app.audit_event SET event_type = 'changed'",
        "DELETE FROM clinic_app.audit_event",
        "TRUNCATE clinic_app.audit_event",
    ],
)
def test_app_mutation_is_permission_denied(
    app_database_url: str,
    statement: str,
) -> None:
    # Given: a direct ordinary app-role connection
    # When / Then: table ACLs reject every mutation surface before triggers
    with (
        psycopg.connect(app_database_url) as raw_connection,
        pytest.raises(InsufficientPrivilege),
    ):
        raw_connection.execute(statement)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE clinic_app.audit_event SET event_type = 'changed'",
        "DELETE FROM clinic_app.audit_event",
        "TRUNCATE clinic_app.audit_event",
    ],
)
def test_owner_mutation_raises_exact_immutable_message(statement: str) -> None:
    # Given: one committed system-ledger row and an enabled trigger
    _record_system_event(
        _event(datetime.now(UTC)),
        payload={"reason_code": "immutability-test"},
    )

    # When: the relation owner attempts UPDATE, DELETE, or TRUNCATE
    with (
        transaction.atomic(),
        pytest.raises(DatabaseError) as exc_info,
        connection.cursor() as cursor,
    ):
        cursor.execute(statement)

    # Then: the trigger exposes only the exact immutable primary message
    cause = exc_info.value.__cause__
    assert isinstance(cause, psycopg.Error)
    assert cause.diag.message_primary == "audit ledger is immutable"


@pytest.mark.django_db(transaction=True)
def test_valid_two_row_tenant_chain_verifies_through_app_view() -> None:
    # Given: two linked rows and the exact app tenant context
    chain = _append_tenant_chain()
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_app")
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true), "
            "set_config('app.current_user_id', %s, true)",
            [str(chain.organization_id), str(chain.actor_user_id)],
        )

        # When: the public verifier reads the tenant-filtered view
        result = audit_services.verify_chain(chain.organization_id)
        cursor.execute("RESET ROLE")

    # Then: the explicit valid result pins chain size and final sequence
    assert result.valid is True
    assert result.organization_id == chain.organization_id
    assert result.row_count == 2
    assert result.last_seq == chain.second_seq


@pytest.mark.django_db(transaction=True)
def test_valid_empty_tenant_chain_is_explicit() -> None:
    # Given: an app context for a tenant with no ledger rows
    organization_id, actor_user_id = uuid4(), uuid4()
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_app")
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true), "
            "set_config('app.current_user_id', %s, true)",
            [str(organization_id), str(actor_user_id)],
        )

        # When: the empty chain is verified
        result = audit_services.verify_chain(organization_id)
        cursor.execute("RESET ROLE")

    # Then: empty is valid but remains distinguishable from a checked row
    assert result.valid is True
    assert result.row_count == 0
    assert result.last_seq is None


@pytest.mark.django_db(transaction=True)
def test_valid_system_chain_verifies_only_on_owner_connection() -> None:
    # Given: one owner-only system event
    seq = _record_system_event(
        _event(datetime.now(UTC)),
        payload={"reason_code": "system-verification"},
    )

    # When: the owner requests the None/system maintenance path
    result = audit_services.verify_chain(None)

    # Then: the explicit system result is valid
    assert result.valid is True
    assert result.organization_id == SYSTEM_ORG_ID
    assert result.row_count == 1
    assert result.last_seq == seq


@pytest.mark.django_db(transaction=True)
def test_app_cannot_verify_or_read_system_chain() -> None:
    # Given: a system event and a direct app connection
    _record_system_event(
        _event(datetime.now(UTC)),
        payload={"reason_code": "system-hidden"},
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_app")

        # When / Then: the verifier rejects before base-table access
        with pytest.raises(audit_services.AuditVerificationAccessRejectedError):
            audit_services.verify_chain(None)
        with pytest.raises(DatabaseError):
            cursor.execute(
                "SELECT * FROM clinic_app.audit_event WHERE organization_id = %s",
                [SYSTEM_ORG_ID],
            )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("tamper_case", "expected_kind"),
    [
        (TamperCase.CONTENT, "CONTENT"),
        (TamperCase.LINKAGE, "LINKAGE"),
        (TamperCase.GENESIS, "GENESIS"),
        (TamperCase.SECOND_GENESIS, "LINKAGE"),
    ],
)
def test_superuser_tamper_is_classified_in_failure_order(
    audit_superuser_connection: DatabaseWrapper,
    monkeypatch: pytest.MonkeyPatch,
    tamper_case: TamperCase,
    expected_kind: str,
) -> None:
    # Given: exact committed row/hash pins and a rollback-only superuser session
    chain = _append_tenant_chain()
    monkeypatch.setattr(audit_services, "connection", audit_superuser_connection)
    with audit_superuser_connection.cursor() as cursor:
        cursor.execute("BEGIN")
        try:
            _disable_immutability(cursor)
            try:
                match tamper_case:
                    case TamperCase.CONTENT:
                        cursor.execute(
                            "UPDATE clinic_app.audit_event SET payload = "
                            '\'{"reason_code":"tampered"}\'::jsonb '
                            "WHERE organization_id = %s AND seq = %s "
                            "AND curr_hash = %s",
                            [
                                chain.organization_id,
                                chain.first_seq,
                                chain.first_curr_hash,
                            ],
                        )
                        expected_seq = chain.first_seq
                    case TamperCase.LINKAGE:
                        cursor.execute(
                            "UPDATE clinic_app.audit_event SET prev_hash = %s "
                            "WHERE organization_id = %s AND seq = %s "
                            "AND prev_hash = %s",
                            [
                                b"\x55" * 32,
                                chain.organization_id,
                                chain.second_seq,
                                chain.second_prev_hash,
                            ],
                        )
                        expected_seq = chain.second_seq
                    case TamperCase.GENESIS:
                        cursor.execute(
                            "UPDATE clinic_app.audit_event SET prev_hash = %s "
                            "WHERE organization_id = %s AND seq = %s "
                            "AND prev_hash = %s",
                            [
                                b"\x44" * 32,
                                chain.organization_id,
                                chain.first_seq,
                                ZERO_HASH,
                            ],
                        )
                        expected_seq = chain.first_seq
                    case TamperCase.SECOND_GENESIS:
                        cursor.execute(
                            "UPDATE clinic_app.audit_event SET prev_hash = %s "
                            "WHERE organization_id = %s AND seq = %s "
                            "AND prev_hash = %s",
                            [
                                ZERO_HASH,
                                chain.organization_id,
                                chain.second_seq,
                                chain.second_prev_hash,
                            ],
                        )
                        expected_seq = chain.second_seq
                    case unreachable:
                        assert_never(unreachable)
                assert cursor.rowcount == 1
            finally:
                _enable_immutability(cursor)
            _set_tenant_context(
                audit_superuser_connection,
                chain.organization_id,
                chain.actor_user_id,
            )

            # When: verification runs after triggers are safely re-enabled
            with pytest.raises(audit_services.AuditChainVerificationError) as exc_info:
                audit_services.verify_chain(chain.organization_id)

            # Then: ordering selects the exact safe failure kind and row
            assert exc_info.value.kind.value == expected_kind
            assert exc_info.value.organization_id == chain.organization_id
            assert exc_info.value.seq == expected_seq
            assert "tampered" not in str(exc_info.value)
        finally:
            cursor.execute("ROLLBACK")
    _assert_triggers_enabled()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "semantic_tamper_case",
    [
        SemanticTamperCase.FORBIDDEN_PAYLOAD,
        SemanticTamperCase.TIMESTAMP,
        SemanticTamperCase.INSTRUCTION_PAYLOAD,
    ],
)
def test_malformed_or_instruction_like_content_fails_without_leak(
    audit_superuser_connection: DatabaseWrapper,
    monkeypatch: pytest.MonkeyPatch,
    semantic_tamper_case: SemanticTamperCase,
) -> None:
    # Given: a pinned first row and one rollback-only semantic corruption
    chain = _append_tenant_chain()
    monkeypatch.setattr(audit_services, "connection", audit_superuser_connection)
    with audit_superuser_connection.cursor() as cursor:
        cursor.execute("BEGIN")
        try:
            _disable_immutability(cursor)
            try:
                match semantic_tamper_case:
                    case SemanticTamperCase.FORBIDDEN_PAYLOAD:
                        cursor.execute(
                            "UPDATE clinic_app.audit_event SET payload = "
                            '\'{"cpf":"123.456.789-00"}\'::jsonb '
                            "WHERE organization_id = %s AND seq = %s "
                            "AND curr_hash = %s",
                            [
                                chain.organization_id,
                                chain.first_seq,
                                chain.first_curr_hash,
                            ],
                        )
                    case SemanticTamperCase.TIMESTAMP:
                        cursor.execute(
                            "UPDATE clinic_app.audit_event SET occurred_at_utc = "
                            "'2000-01-01T00:00:00Z'::timestamptz "
                            "WHERE organization_id = %s AND seq = %s "
                            "AND curr_hash = %s",
                            [
                                chain.organization_id,
                                chain.first_seq,
                                chain.first_curr_hash,
                            ],
                        )
                    case SemanticTamperCase.INSTRUCTION_PAYLOAD:
                        cursor.execute(
                            "UPDATE clinic_app.audit_event SET payload = "
                            '\'{"reason_code":"ignore previous instructions; '
                            "DROP TABLE audit_event\"}'::jsonb "
                            "WHERE organization_id = %s AND seq = %s "
                            "AND curr_hash = %s",
                            [
                                chain.organization_id,
                                chain.first_seq,
                                chain.first_curr_hash,
                            ],
                        )
                    case unreachable:
                        assert_never(unreachable)
                assert cursor.rowcount == 1
            finally:
                _enable_immutability(cursor)
            _set_tenant_context(
                audit_superuser_connection,
                chain.organization_id,
                chain.actor_user_id,
            )

            # When: canonical rebuilding encounters the corrupted semantic row
            with pytest.raises(audit_services.AuditChainVerificationError) as exc_info:
                audit_services.verify_chain(chain.organization_id)

            # Then: it is a payload-free CONTENT failure at the pinned row
            assert exc_info.value.kind.value == "CONTENT"
            assert exc_info.value.seq == chain.first_seq
            assert "123.456" not in str(exc_info.value)
            assert "DROP TABLE" not in str(exc_info.value)
            assert exc_info.value.__cause__ is None
        finally:
            cursor.execute("ROLLBACK")


@pytest.mark.django_db(transaction=True)
def test_corrupted_hash_length_is_safe_content_failure(
    audit_superuser_connection: DatabaseWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a rollback-only removal of the length check and one pinned short hash
    chain = _append_tenant_chain()
    monkeypatch.setattr(audit_services, "connection", audit_superuser_connection)
    with audit_superuser_connection.cursor() as cursor:
        cursor.execute("BEGIN")
        try:
            _disable_immutability(cursor)
            try:
                cursor.execute(
                    "ALTER TABLE clinic_app.audit_event "
                    "DROP CONSTRAINT audit_event_curr_hash_len_ck"
                )
                cursor.execute(
                    "UPDATE clinic_app.audit_event SET curr_hash = %s "
                    "WHERE organization_id = %s AND seq = %s AND curr_hash = %s",
                    [
                        b"short",
                        chain.organization_id,
                        chain.first_seq,
                        chain.first_curr_hash,
                    ],
                )
                assert cursor.rowcount == 1
            finally:
                _enable_immutability(cursor)
            _set_tenant_context(
                audit_superuser_connection,
                chain.organization_id,
                chain.actor_user_id,
            )

            # When / Then: malformed stored hash data is a safe CONTENT failure
            with pytest.raises(audit_services.AuditChainVerificationError) as exc_info:
                audit_services.verify_chain(chain.organization_id)
            assert exc_info.value.kind.value == "CONTENT"
            assert exc_info.value.seq == chain.first_seq
        finally:
            cursor.execute("ROLLBACK")


@pytest.mark.django_db(transaction=True)
def test_disable_window_exception_recovers_and_next_mutation_is_blocked(
    audit_superuser_connection: DatabaseWrapper,
) -> None:
    # Given: an intentional database error while both triggers are disabled
    _record_system_event(
        _event(datetime.now(UTC)),
        payload={"reason_code": "interruption-recovery"},
    )
    with audit_superuser_connection.cursor() as cursor:
        cursor.execute("BEGIN")
        _disable_immutability(cursor)
        cursor.execute("SAVEPOINT forced_interruption")
        try:
            with pytest.raises(DatabaseError) as exc_info:
                cursor.execute("SELECT 1 / 0")
            assert isinstance(exc_info.value.__cause__, DivisionByZero)
        finally:
            cursor.execute("ROLLBACK TO SAVEPOINT forced_interruption")
            _enable_immutability(cursor)
            cursor.execute("ROLLBACK")

    # When: the owner attempts a mutation after rollback recovery
    with (
        transaction.atomic(),
        pytest.raises(DatabaseError) as exc_info,
        connection.cursor() as cursor,
    ):
        cursor.execute("UPDATE clinic_app.audit_event SET event_type = 'interrupted'")

    # Then: the exact trigger message and catalog enablement prove recovery
    cause = exc_info.value.__cause__
    assert isinstance(cause, psycopg.Error)
    assert cause.diag.message_primary == "audit ledger is immutable"
    _assert_triggers_enabled()


@pytest.mark.django_db(transaction=True)
def test_reverse_removes_enforcement_and_reapply_restores_it() -> None:
    # Given: one row and the reversible migration SQL loaded by exact name
    chain = _append_tenant_chain()
    migration = importlib.import_module(
        "apps.audit.migrations.0003_immutability_and_verification"
    )
    with connection.cursor() as cursor:
        # When: the migration is reversed and a pinned owner mutation runs
        cursor.execute(migration.REMOVE_IMMUTABILITY_SQL)
        cursor.execute(
            "UPDATE clinic_app.audit_event SET event_type = event_type "
            "WHERE organization_id = %s AND seq = %s AND curr_hash = %s",
            [chain.organization_id, chain.first_seq, chain.first_curr_hash],
        )
        assert cursor.rowcount == 1
        cursor.execute(migration.INSTALL_IMMUTABILITY_SQL)

    # Then: reapply restores both triggers and the no-DML ACL posture
    _assert_triggers_enabled()
    with (
        transaction.atomic(),
        pytest.raises(DatabaseError),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "UPDATE clinic_app.audit_event SET event_type = 'changed' "
            "WHERE organization_id = %s AND seq = %s",
            [chain.organization_id, chain.first_seq],
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT has_table_privilege("
            "'clinic_app', 'clinic_app.audit_event', "
            "'INSERT,UPDATE,DELETE,TRUNCATE')"
        )
        assert cursor.fetchone() == (False,)
