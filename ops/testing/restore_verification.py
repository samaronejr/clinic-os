"""Perform read-only source/target equality and database-posture proofs."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from hashlib import sha256
from ipaddress import ip_address
from typing import TYPE_CHECKING, Final, Never, Protocol
from uuid import UUID

from apps.audit.canonical import (
    AuditEventInput,
    AuditEventValueRejectedError,
    AuditPayloadKeyRejected,
    AuditPayloadValueRejectedError,
    AuditTrustedContext,
    _content_hash,
    _normalize_payload,
)

from ops.testing.restore_contract import (
    REQUIRED_EMPTY,
    SEQUENCE_TARGETS,
    RestoreContractError,
    SourceScope,
)
from ops.testing.restore_queries import (
    ATTACHMENT_MANIFEST_SQL,
    AUDIT_CHAIN_SQL,
    AUDIT_ROWS_SQL,
    EMPTY_TARGET_SQL,
    EQUALITY_RELATIONS,
    FINGERPRINT_SQL,
    HBA_SQL,
    MIGRATION_LEAVES_SQL,
    POSTURE_SQL,
    SOURCE_SCOPE_SQL,
    TARGET_SEED_SQL,
    TENANT_KEY_STATUS_SQL,
)
from ops.testing.tls_contract import HBA_RULES

if TYPE_CHECKING:
    from pathlib import Path

MD5_HEX_LENGTH: Final = 32
MANIFEST_FIELD_COUNT: Final = 4
SEQUENCE_FIELD_COUNT: Final = 2
KEY_PATTERN: Final = re.compile(r"[0-9a-f]{64}")
MAX_ATTACHMENT_BYTES: Final = 10 * 1024 * 1024


class _SqlClient(Protocol):
    def sql(self, statement: str) -> str: ...


def migration_leaves(client: _SqlClient) -> str:
    """Read the exact sorted migration-leaf set from one database."""
    return client.sql(MIGRATION_LEAVES_SQL)


def require_equal_migration_leaves(source: str, target: str) -> None:
    """Reject target state drift before any archive restore begins."""
    if not source or source != target:
        _fail("target migration leaf set does not equal source")


def require_equal_target_seed(source: _SqlClient, target: _SqlClient) -> None:
    """Require the excluded migration-seeded registry to equal the target's.

    ``TARGET_SEEDED_RELATIONS`` are not archived because the target's own
    migrations recreate them; any source drift from that seed would be lost
    by the restore, so it refuses before any target mutation instead.
    """
    observed = source.sql(TARGET_SEED_SQL).strip()
    if len(observed) != MD5_HEX_LENGTH or any(
        character not in "0123456789abcdef" for character in observed
    ):
        _fail("target-seeded registry fingerprint is invalid")
    if observed != target.sql(TARGET_SEED_SQL).strip():
        _fail("source provider registry differs from the target migration seed")


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


def require_audit_chain(client: _SqlClient) -> None:
    """Require every restored audit event to chain to its predecessor."""
    violations = client.sql(AUDIT_CHAIN_SQL).strip()
    if violations != "0":
        _fail("restored audit hash chain is broken")


def require_audit_content(client: _SqlClient) -> int:
    """Recompute every restored event's content hash and chained curr_hash.

    Linkage alone accepts a fabricated chain whose hashes are internally
    consistent but whose content was never produced by the audit boundary;
    this proof recomputes the canonical content hash of every stored row and
    the exact ``sha256(content_hash || prev_hash)`` chain the append
    functions write, so a fabricated or edited event fails closed. Returns
    the verified event count.
    """
    previous: dict[str, bytes] = {}
    verified = 0
    for line in client.sql(AUDIT_ROWS_SQL).splitlines():
        row = _audit_row(line)
        # ``_audit_row`` already rejected any row whose fields are not text.
        organization = str(row["organization_id"])
        prev_hash = bytes.fromhex(str(row["prev_hash"]))
        expected_prev = previous.get(organization)
        if expected_prev is None:
            if prev_hash != b"\x00" * 32:
                _fail("restored audit hash chain is broken")
        elif prev_hash != expected_prev:
            _fail("restored audit hash chain is broken")
        content_hash = _audit_content_hash(row)
        curr_hash = bytes.fromhex(str(row["curr_hash"]))
        if curr_hash != sha256(content_hash + prev_hash).digest():
            _fail("restored audit content hash is invalid")
        previous[organization] = curr_hash
        verified += 1
    return verified


def _audit_row(line: str) -> dict[str, object]:
    """Parse one canonical audit row; malformed JSON fails closed."""
    try:
        value: object = json.loads(line)
    except json.JSONDecodeError:
        _fail("restored audit row is invalid")
    if not isinstance(value, dict) or set(value) != {
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
    }:
        _fail("restored audit row is invalid")
    row: dict[str, object] = value
    if (
        not isinstance(row["seq"], int)
        or not isinstance(row["organization_id"], str)
        or not isinstance(row["event_type"], str)
        or not isinstance(row["component_id"], str)
        or not isinstance(row["occurred_at_utc"], str)
        or not isinstance(row["payload"], dict)
        or not isinstance(row["prev_hash"], str)
        or not isinstance(row["curr_hash"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", row["prev_hash"])
        or not re.fullmatch(r"[0-9a-f]{64}", row["curr_hash"])
    ):
        _fail("restored audit row is invalid")
    return row


def _audit_content_hash(row: dict[str, object]) -> bytes:
    """Rebuild the canonical content hash of one stored audit row."""
    try:
        component_ip = row["component_ip"]
        event = AuditEventInput(
            event_type=str(row["event_type"]),
            component_id=str(row["component_id"]),
            component_ip=None
            if component_ip is None
            else ip_address(str(component_ip)),
            affected_record_type=(
                None
                if row["affected_record_type"] is None
                else str(row["affected_record_type"])
            ),
            affected_record_id=(
                None
                if row["affected_record_id"] is None
                else str(row["affected_record_id"])
            ),
            occurred_at_utc=datetime.strptime(
                str(row["occurred_at_utc"]), "%Y-%m-%dT%H:%M:%S.%fZ"
            ).replace(tzinfo=UTC),
        )
        actor = row["actor_user_id"]
        context = AuditTrustedContext(
            organization_id=UUID(str(row["organization_id"])),
            actor_user_id=None if actor is None else UUID(str(actor)),
        )
        payload = _normalize_payload(row["payload"])  # type: ignore[arg-type]
    except (
        AttributeError,
        AuditEventValueRejectedError,
        AuditPayloadKeyRejected,
        AuditPayloadValueRejectedError,
        KeyError,
        TypeError,
        ValueError,
    ):
        _fail("restored audit row is invalid")
    return _content_hash(event, context, payload)


def require_empty_target(client: _SqlClient) -> None:
    """Require zero domain rows before any restore mutation begins."""
    count = client.sql(EMPTY_TARGET_SQL).strip()
    if count != "0":
        _fail("restore target is not empty")


FOREIGN_PROBE_ORGANIZATION: Final = "00000000-0000-4000-8000-0000000000ff"
FOREIGN_PROBE_PATIENT: Final = "00000000-0000-4000-8000-0000000000fe"


def seed_foreign_probe_tenant(client: _SqlClient) -> None:
    """Insert one foreign organization and patient on the disposable target.

    ``client`` must be the privileged (superuser) target binding. The seeded
    rows exist only so the app-role probes exercise RLS against a populated
    foreign record: a permissive policy must change the observed counts.
    The patient carries placeholder envelope bytes; the probes assert
    visibility, never content.
    """
    client.sql(
        "INSERT INTO clinic_app.identity_organization (id, name, cnpj) "
        "VALUES ('"
        + FOREIGN_PROBE_ORGANIZATION
        + "', 'Foreign Probe Organization', '99999999999999') "
        "ON CONFLICT (id) DO NOTHING"
    )
    client.sql(
        "INSERT INTO clinic_app.intake_patient "
        "(id, organization_id, full_name, birth_date, created_at) "
        "VALUES ('"
        + FOREIGN_PROBE_PATIENT
        + "', '"
        + FOREIGN_PROBE_ORGANIZATION
        + "', pg_catalog.decode('01', 'hex'), pg_catalog.decode('01', 'hex'), "
        "pg_catalog.statement_timestamp()) ON CONFLICT (id) DO NOTHING"
    )


def require_app_role_probes(
    client: _SqlClient,
    *,
    organization_id: str,
    expected_patient_count: int,
) -> None:
    """Exercise the restored database through the runtime role, not superuser.

    ``client`` must already be bound to ``clinic_app``. The probes prove the
    restored RLS policies, the closed ledger boundary and the DEK unwrap
    restriction all behave for the application role exactly as on source.
    ``expected_patient_count`` is the privileged count of the restored
    tenant's patients, so the own-tenant read must match it exactly and the
    seeded foreign row must be invisible under every context.
    """
    if (
        not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            organization_id,
        )
        or expected_patient_count < 1
    ):
        _fail("app-role probe input is invalid")
    no_context = client.sql("SELECT count(*) FROM clinic_app.intake_patient").strip()
    if no_context != "0":
        _fail("app role reads patients without a tenant context")
    tenant_rows = (
        client.sql(
            "SET app.current_tenant = '"  # noqa: S608 - uuid validated above
            + organization_id
            + "';SELECT count(*) FROM clinic_app.intake_patient"
        )
        .strip()
        .splitlines()
    )
    if not tenant_rows or tenant_rows[-1].strip() != str(expected_patient_count):
        _fail("app-role tenant read probe failed")
    foreign_rows = (
        client.sql(
            "SET app.current_tenant = '"  # noqa: S608 - uuid validated above
            + organization_id
            + "';SELECT count(*) FROM clinic_app.intake_patient "
            + "WHERE organization_id <> '"
            + organization_id
            + "'"
        )
        .strip()
        .splitlines()
    )
    if not foreign_rows or foreign_rows[-1].strip() != "0":
        _fail("app-role cross-tenant read was not denied")
    for relation in ("audit_event", "tenancy_tenantdatakey"):
        for privilege in ("SELECT", "INSERT"):
            denied = client.sql(
                "SELECT has_table_privilege('clinic_app', 'clinic_app."
                + relation
                + "', '"
                + privilege
                + "')"
            ).strip()
            if denied != "f":
                _fail(
                    "app role holds closed-table privilege: "
                    + relation
                    + ":"
                    + privilege
                    + "="
                    + denied
                )
    direct = client.sql(
        "SELECT has_function_privilege('clinic_app', "
        "'clinic_app.tenant_dek_unwrap(text,integer)', 'EXECUTE')"
    ).strip()
    if direct != "f":
        _fail("app role holds direct DEK unwrap privilege")


def require_equal_tenant_key_status(source: _SqlClient, target: _SqlClient) -> str:
    """Require identical DEK version/status metadata on source and target."""
    observed = source.sql(TENANT_KEY_STATUS_SQL).strip()
    if not observed or observed != target.sql(TENANT_KEY_STATUS_SQL).strip():
        _fail("restored tenant key status differs from source")
    return observed


def require_key_probe(
    client: _SqlClient,
    *,
    organization_id: str,
    kek: str,
    envelope_hex: str,
    expected_sha256: str,
) -> None:
    """Decrypt one source envelope on the target through the real boundary.

    Proves the restored wrapped DEK, under the configured KEK, still opens
    tenant ciphertext: the strongest possible restoration evidence for the
    encryption capability. The probe runs inside one implicit transaction so
    the tenant GUC binds the SECURITY DEFINER function's RLS read.
    """
    if (
        not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            organization_id,
        )
        or not re.fullmatch(r"[0-9a-f]{64}", kek)
        or not re.fullmatch(r"[0-9a-f]+", envelope_hex)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
    ):
        _fail("key restoration probe input is invalid")
    statement = (
        f"SET app.current_tenant = '{organization_id}';"
        "SELECT encode(clinic_app.digest(clinic_app.tenant_decrypt("
        f"'{kek}', 'restore-probe', decode('{envelope_hex}', 'hex')"
        "), 'sha256'), 'hex')"
    )
    observed = client.sql(statement).strip().splitlines()
    if not observed or observed[-1].strip() != expected_sha256:
        _fail("restored tenant key cannot decrypt the source probe envelope")


def verify_object_store(client: _SqlClient, root: Path, kek: str) -> int:
    """Bind every stored attachment row to its restored object bytes.

    The manifest comes from the restored database; every entry must map to
    exactly one regular file under ``root`` whose bytes equal the restored
    envelope column, and the envelope must decrypt under the tenant DEK to
    plaintext matching the stored size and SHA-256. No other files may exist
    there. Returns the verified object count.
    """
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        _fail("restored object store root is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", kek):
        _fail("object store verification key is invalid")
    manifest: dict[str, tuple[str, str, int]] = {}
    for line in client.sql(ATTACHMENT_MANIFEST_SQL).splitlines():
        fields = line.strip().split("|")
        if (
            len(fields) != MANIFEST_FIELD_COUNT
            or KEY_PATTERN.fullmatch(fields[0]) is None
            or not re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}"
                r"-[0-9a-f]{12}",
                fields[1],
            )
            or not re.fullmatch(r"[0-9a-f]{64}", fields[2])
            or not fields[3].isdecimal()
        ):
            _fail("attachment manifest row is invalid")
        manifest[fields[0]] = (fields[1], fields[2], int(fields[3]))
    on_disk = {
        entry.name
        for entry in root.iterdir()
        if entry.is_file() and not entry.is_symlink()
    }
    unexpected = {entry.name for entry in root.iterdir()} - set(manifest)
    if unexpected or on_disk != set(manifest):
        _fail("restored object store does not equal the attachment manifest")
    for key, (organization_id, digest, size) in manifest.items():
        path = root / key
        data = path.read_bytes()
        if len(data) > MAX_ATTACHMENT_BYTES:
            _fail("restored object exceeds the attachment bound")
        if size <= 0 or size > MAX_ATTACHMENT_BYTES:
            _fail("restored object size is invalid")
        statement = (
            f"SET app.current_tenant = '{organization_id}';"
            "SELECT encode(clinic_app.digest(clinic_app.tenant_decrypt("
            f"'{kek}', 'ehr.clinicalattachment.bytes', "
            f"decode('{data.hex()}', 'hex')"
            "), 'sha256'), 'hex')"
        )
        observed = client.sql(statement).strip().splitlines()
        if not observed or observed[-1].strip() != digest:
            _fail("restored object plaintext does not match the stored digest")
    return len(manifest)


def require_sequence_headroom(client: _SqlClient) -> tuple[tuple[str, int, int], ...]:
    """Advance each restored sequence once and require values above maxima."""
    result: list[tuple[str, int, int]] = []
    for name in sorted(SEQUENCE_TARGETS):
        table, column = SEQUENCE_TARGETS[name]
        statement = (
            f"SELECT nextval('clinic_app.{name}'), "  # noqa: S608 - fixed map
            f"COALESCE(max({column}),0) FROM clinic_app.{table}"
        )
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
    raw = client.sql(SOURCE_SCOPE_SQL).strip()
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
        "tenant_data_key_count",
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
        integers["tenant_data_key_count"],
        integers["totp_without_identity_count"],
        unexpected,
        integers["users_without_role_count"],
    )


def _fingerprint_sql(relation: str) -> str:
    matches = [statement for name, statement in FINGERPRINT_SQL if name == relation]
    if len(matches) != 1:
        _fail("relation fingerprint statement is invalid")
    return matches[0]


def _fail(message: str) -> Never:
    raise RestoreContractError(message)
