"""Perform read-only source/target equality and database-posture proofs."""

from __future__ import annotations

import json
from typing import Final, Never, Protocol

from ops.testing.restore_contract import (
    REQUIRED_EMPTY,
    RestoreContractError,
    SourceScope,
)
from ops.testing.restore_queries import (
    EQUALITY_RELATIONS,
    FINGERPRINT_SQL,
    HBA_SQL,
    MIGRATION_LEAVES_SQL,
    POSTURE_SQL,
    SOURCE_SCOPE_SQL,
)
from ops.testing.tls_contract import HBA_RULES

MD5_HEX_LENGTH: Final = 32
SEQUENCE_FIELD_COUNT: Final = 2


class _SqlClient(Protocol):
    def sql(self, statement: str) -> str: ...


def migration_leaves(client: _SqlClient) -> str:
    """Read the exact sorted migration-leaf set from one database."""
    return client.sql(MIGRATION_LEAVES_SQL)


def require_equal_migration_leaves(source: str, target: str) -> None:
    """Reject target state drift before any archive restore begins."""
    if not source or source != target:
        _fail("target migration leaf set does not equal source")


def relation_fingerprints(client: _SqlClient) -> dict[str, str]:
    """Hash every restored row in-database without exporting row values."""
    result: dict[str, str] = {}
    for relation in EQUALITY_RELATIONS:
        digest = client.sql(_fingerprint_sql(relation)).strip()
        if len(digest) != MD5_HEX_LENGTH or any(
            character not in "0123456789abcdef" for character in digest
        ):
            _fail("relation fingerprint is invalid")
        result[relation] = digest
    return result


def require_equal_fingerprints(source: dict[str, str], target: dict[str, str]) -> None:
    """Require byte-equivalent restored tables through read-only hashes."""
    if set(source) != set(EQUALITY_RELATIONS) or source != target:
        _fail("restored read-only equality proof failed")


def require_owner_rls_acl_posture(client: _SqlClient) -> None:
    """Require owner/resolver ownership and FORCE-RLS declarations."""
    violations = client.sql(POSTURE_SQL).strip()
    if violations:
        _fail(f"restored posture violations: {violations}")


def require_sequence_headroom(client: _SqlClient) -> tuple[tuple[str, int, int], ...]:
    """Advance both restored sequences once and require values above maxima."""
    statements = (
        (
            "audit_event_seq_seq",
            "SELECT nextval('clinic_app.audit_event_seq_seq'), "
            "COALESCE(max(seq),0) FROM clinic_app.audit_event",
        ),
        (
            "otp_totp_totpdevice_id_seq",
            "SELECT nextval('clinic_app.otp_totp_totpdevice_id_seq'), "
            "COALESCE(max(id),0) FROM clinic_app.otp_totp_totpdevice",
        ),
    )
    result: list[tuple[str, int, int]] = []
    for name, statement in statements:
        fields = client.sql(statement).strip().split("|")
        if len(fields) != SEQUENCE_FIELD_COUNT or not all(
            field.isdecimal() for field in fields
        ):
            _fail("restored sequence observation is invalid")
        next_value, maximum = int(fields[0]), int(fields[1])
        if next_value <= maximum:
            _fail("restored sequence does not exceed its table maximum")
        result.append((name, next_value, maximum))
    return tuple(result)


def require_hba(client: _SqlClient) -> None:
    """Require the exact six TLS/reject HBA rules and no parser error."""
    raw = client.sql(HBA_SQL)
    observed = tuple(raw.splitlines())
    if observed != HBA_RULES:
        _fail(f"PostgreSQL HBA rules differ: {observed!r}")


def source_scope(client: _SqlClient) -> SourceScope:
    """Read only aggregate scope counters from the stopped source."""
    raw = client.sql(_source_scope_sql()).strip()
    value: object = json.loads(raw)
    if not isinstance(value, dict):
        _fail("source scope observation is invalid")
    integers: dict[str, int] = {}
    unexpected: tuple[str, ...] = ()
    for key, item in value.items():
        if key == "unexpected_domain_relations":
            if not isinstance(item, list) or not all(
                isinstance(entry, str) for entry in item
            ):
                _fail("source scope observation is invalid")
            unexpected = tuple(entry for entry in item if isinstance(entry, str))
        elif (
            isinstance(key, str)
            and isinstance(item, int)
            and not isinstance(item, bool)
        ):
            integers[key] = item
        else:
            _fail("source scope observation is invalid")
    required = {
        "active_writer_count",
        "foreign_audit_organization_count",
        "identity_user_count",
        "organization_count",
        "totp_without_identity_count",
        "users_without_role_count",
        *REQUIRED_EMPTY,
    }
    if set(integers) != required:
        _fail("source scope observation is invalid")
    empty = {name: integers[name] for name in REQUIRED_EMPTY}
    return SourceScope(
        integers["active_writer_count"],
        empty,
        integers["foreign_audit_organization_count"],
        integers["identity_user_count"],
        integers["organization_count"],
        integers["totp_without_identity_count"],
        unexpected,
        integers["users_without_role_count"],
    )


def _source_scope_sql() -> str:
    return SOURCE_SCOPE_SQL


def _fingerprint_sql(relation: str) -> str:
    matches = [statement for name, statement in FINGERPRINT_SQL if name == relation]
    if len(matches) != 1:
        _fail("relation fingerprint statement is invalid")
    return matches[0]


def _fail(message: str) -> Never:
    raise RestoreContractError(message)
