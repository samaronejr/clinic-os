"""Capture the seed's actual write set and verify every value after migration."""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, cast

import psycopg
from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from psycopg import Cursor

type Row = dict[str, object]
type Tables = dict[str, dict[str, Row]]
_TYPED = "__upgrade_typed_values__"
_ORIGINAL_AND_ONE_RETRY = 2

# Metadata and reference rows are created by migrations, not by the seed.
# Every other product table is checked, including indirect service writes;
# a new fixture model cannot accidentally escape a hand-maintained seed list.
_REFERENCE = (
    "django_migrations",
    "django_content_type",
    "auth_permission",
    "providers_providercapability",
    "providers_capabilityversion",
    "prescription_prescriptionclasspolicy",
)

_DEFAULT_COLUMNS: dict[tuple[str, str], dict[str, Row]] = {
    ("identity", "0016_task_workflows"): {
        "identity_userclinicrole": {"workflow_bundle_version": 1},
    },
    ("identity", "0018_signup"): {"identity_organization": {"status": "active"}},
    ("identity", "0019_identity_lifecycle"): {
        "identity_userclinicrole": {"revoked_at": None},
    },
    ("identity", "0036_passkey_sessions"): {
        "identity_organization": {"passkey_step_up_enabled": False},
    },
    ("scheduling", "0005_resources_templates"): {
        "scheduling_appointment": {
            "buffer_after": 0,
            "buffer_before": 0,
            "resource_ids": [],
            "service_type_id": None,
        },
        "scheduling_availabilityblock": {
            "generated_date": None,
            "resource_id": None,
            "template_id": None,
        },
    },
    ("scheduling", "0007_appointment_lifecycle_v2"): {
        "scheduling_appointment": {
            "authorization_reference": "",
            "hold_expires_at": None,
            "last_command_id": None,
            "payer_membership_id": None,
            "revision": 1,
            "series_index": None,
            "series_id": None,
            "transitioned_at": None,
        },
    },
    ("scheduling", "0011_insurer_membership"): {
        "scheduling_appointment": {
            "payer_contract_id": None,
            "eligibility_check_id": None,
            "authorization_check_id": None,
        },
    },
    ("ai", "0014_staged_rollout"): {
        "ai_gatewayswitch": {"generation": 0},
        "ai_evaluationrun": {"stage": "offline", "rollout_approval_id": None},
        "ai_modelchangerequest": {
            "stage": "shadow",
            "exposure": "none",
            "switch_generation": 0,
            "authority_sha256": "",
        },
        "ai_aiinvocation": {"rollout_approval_id": None},
        "ai_agentrun": {"rollout_approval_id": None},
    },
    ("ai", "0015_front_desk_rollout"): {
        "ai_frontdeskturn": {"rollout_approval_id": None, "source_xid": ""},
    },
    ("ai", "0016_retained_effects"): {"ai_aiinvocation": {"effect_xid": ""}},
    ("ai", "0017_retained_proposal_sources"): {"ai_actionproposal": {"source_xid": ""}},
    ("comms", "0012_attempt_delivery"): {
        "comms_inboundevent": {"operation_id": None, "delivery_state": ""},
    },
    ("comms", "0013_attempt_sequence"): {
        "comms_integrationoperation": {"attempt_sequence": None},
    },
    ("comms", "0015_send_permit"): {
        "comms_integrationoperation": {"send_permit": None}
    },
    ("comms", "0017_inflight_claim"): {
        "comms_integrationoperation": {"send_permit_claim": None},
    },
}


def _message_attempts(rows: Tables) -> list[Row]:
    return [
        row
        for row in rows.get("comms_integrationoperation", {}).values()
        if row["subject_type"] == "comms.message"
    ]


def _by_id(rows: Tables, table: str, identity: object) -> Row:
    return rows[table][json.dumps([identity], separators=(",", ":"))]


def _attempt_sequence(rows: Tables) -> None:
    attempts = _message_attempts(rows)
    for event in rows.get("comms_inboundevent", {}).values():
        if event["kind"] != "status" or event["operation_id"] is not None:
            continue
        match = re.fullmatch(
            r"(.+):(sent|delivered|read|failed):[^:]*", str(event["event_id"])
        )
        if match is None:
            continue
        for attempt in attempts:
            if (
                attempt["channel"] != "whatsapp"
                or attempt["organization_id"] != event["organization_id"]
                or attempt["provider_reference"] != match[1]
            ):
                continue
            message = _by_id(rows, "comms_message", attempt["subject_id"])
            conversation = _by_id(
                rows, "comms_conversation", message["conversation_id"]
            )
            if conversation["provider_account_id"] == event["provider_account_id"]:
                event.update(operation_id=attempt["id"], delivery_state=match[2])
                break
    for attempt in attempts:
        message = _by_id(rows, "comms_message", attempt["subject_id"])
        group = [a for a in attempts if a["subject_id"] == attempt["subject_id"]]
        originals = sum(a["idempotency_key"] == message["command_id"] for a in group)
        unattributed = any(a["channel"] != "whatsapp" for a in group)
        if len(group) == 1 or (
            len(group) == _ORIGINAL_AND_ONE_RETRY
            and originals == 1
            and not (unattributed and message["provider_state"] == "failed")
        ):
            attempt["attempt_sequence"] = (
                1
                if len(group) == 1
                or attempt["idempotency_key"] == message["command_id"]
                else 2
            )


def _has_failed_receipt(rows: Tables, attempt: Row) -> bool:
    return any(
        event["operation_id"] == attempt["id"] and event["delivery_state"] == "failed"
        for event in rows.get("comms_inboundevent", {}).values()
    )


def _send_permit(rows: Tables) -> None:
    attempts = _message_attempts(rows)
    for attempt in attempts:
        message = _by_id(rows, "comms_message", attempt["subject_id"])
        original = attempt["idempotency_key"] == message["command_id"]
        in_flight = attempt["status"] == "in_progress"
        if original and any(
            a["subject_id"] == attempt["subject_id"] and a["id"] != attempt["id"]
            for a in attempts
        ):
            attempt["send_permit"] = "released"
        elif attempt["attempt_sequence"] is None:
            if in_flight:
                attempt["send_permit"] = "held"
        elif (attempt["provider_reference"] is not None or in_flight) and not (
            attempt["status"] in ("failed", "cancelled")
            or _has_failed_receipt(rows, attempt)
            or (original and message["provider_state"] == "failed")
        ):
            attempt["send_permit"] = "held"


def _permit_evidence(rows: Tables) -> None:
    for attempt in _message_attempts(rows):
        message = _by_id(rows, "comms_message", attempt["subject_id"])
        if attempt["idempotency_key"] == message["command_id"]:
            if message["provider_state"] == "failed":
                attempt["send_permit"] = "released"
        elif (
            attempt["send_permit"] == "released"
            and attempt["provider_reference"] is not None
            and not _has_failed_receipt(rows, attempt)
        ):
            attempt["send_permit"] = "open"


def _session_claim(rows: Tables) -> None:
    for attempt in _message_attempts(rows):
        if (
            attempt["status"] == "in_progress"
            and attempt["send_permit"] == "held"
            and attempt["provider_reference"] is None
        ):
            attempt["send_permit_claim"] = (
                "\\x" + sha256(b"legacy-in-flight").hexdigest()
            )


def _patient_name_index(rows: Tables) -> None:
    from django.db import connection, transaction  # noqa: PLC0415

    # Independent vector for the defined fixture name, not normalization of
    # the row under test and not a call to the migration's Python helper.
    normalized = b"synthetic upgrade persona"
    kek = (
        (Path(os.environ["CLINIC_SECRET_DIR"]) / "tenant-kek.secret")
        .read_text(encoding="ascii")
        .strip()
    )
    for patient in rows.get("intake_patient", {}).values():
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant', %s, true)",
                [str(patient["organization_id"])],
            )
            cursor.execute(
                "SELECT clinic_app.hmac(%s,"
                "clinic_app.tenant_dek_unwrap(%s, key_version), 'sha256') "
                "FROM clinic_app.tenancy_tenantdatakey "
                "ORDER BY key_version DESC LIMIT 1",
                [b"intake.patient.full_name.exact.v1\0" + normalized, kek],
            )
            patient["full_name_index"] = "\\x" + bytes(cursor.fetchone()[0]).hex()


# The only data rewrites in the supported base paths, in migration order.
# Unlisted columns remain byte/value-identical, including encrypted fields.
_REWRITES: dict[tuple[str, str], tuple[tuple[str, ...], Callable[[Tables], None]]] = {
    ("intake", "0012_patient_name_index"): (
        ("intake_patient.full_name_index",),
        _patient_name_index,
    ),
    ("comms", "0013_attempt_sequence"): (
        (
            "comms_inboundevent.operation_id",
            "comms_inboundevent.delivery_state",
            "comms_integrationoperation.attempt_sequence",
        ),
        _attempt_sequence,
    ),
    ("comms", "0015_send_permit"): (
        ("comms_integrationoperation.send_permit",),
        _send_permit,
    ),
    ("comms", "0016_permit_evidence"): (
        ("comms_integrationoperation.send_permit",),
        _permit_evidence,
    ),
    ("comms", "0018_session_claim"): (
        ("comms_integrationoperation.send_permit_claim",),
        _session_claim,
    ),
}


def expected_rows(rows: Tables, applied: set[tuple[str, str]]) -> Tables:
    """Apply reviewed defaults and transforms to the base fixture values."""
    for migration in sorted(_DEFAULT_COLUMNS.keys() | _REWRITES.keys()):
        if migration not in applied:
            continue
        for table, values in _DEFAULT_COLUMNS.get(migration, {}).items():
            for row in rows.get(table, {}).values():
                row.update(values)
        if migration in _REWRITES:
            columns, transform = _REWRITES[migration]
            before = deepcopy(rows)
            transform(rows)
            changed = differences(before, rows)
            if any(failure.split(" ", 4)[3] not in columns for failure in changed):
                message = f"undeclared fixture rewrite in {migration}"
                raise RuntimeError(message)
            for path in columns:
                table, column = path.split(".", 1)
                for row in rows.get(table, {}).values():
                    cast("dict[str, str]", row.get(_TYPED, {})).pop(column, None)
    return rows


def table_keys(cursor: Cursor[tuple[object, ...]]) -> list[tuple[str, list[str]]]:
    """Discover potential seed tables and stable ordered primary keys."""
    cursor.execute(
        """
        SELECT c.relname, ARRAY(
            SELECT a.attname FROM pg_index i
            CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, n)
            JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=k.attnum
            WHERE i.indrelid=c.oid AND i.indisprimary ORDER BY k.n
        )
        FROM pg_class c WHERE c.relnamespace='clinic_app'::regnamespace
        AND c.relkind IN ('r','p') AND NOT c.relname=ANY(%s) ORDER BY c.relname
        """,
        [list(_REFERENCE)],
    )
    return [
        (str(name), list(cast("Sequence[str]", keys)))
        for name, keys in cursor.fetchall()
    ]


def read_rows(cursor: Cursor[tuple[object, ...]], *, typed: bool = False) -> Tables:
    """Read all fixture-eligible rows through the scratch admin audit connection."""
    result: Tables = {}
    for table, keys in table_keys(cursor):
        names: list[str] = []
        if typed:
            cursor.execute(
                "SELECT attname FROM pg_attribute "
                "WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped "
                "ORDER BY attnum",
                ["clinic_app." + table],
            )
            names = [str(name) for (name,) in cursor.fetchall()]
        expressions: list[sql.Composable] = [sql.SQL("to_jsonb(t)::text")]
        expressions.extend(
            sql.SQL("encode(pg_catalog.record_send(ROW(t.{})), 'hex')").format(
                sql.Identifier(name)
            )
            for name in names
        )
        cursor.execute(
            sql.SQL("SELECT {} FROM clinic_app.{} t").format(
                sql.SQL(", ").join(expressions), sql.Identifier(table)
            )
        )
        rows: dict[str, Row] = {}
        for raw, *values in cursor:
            if not keys:
                message = f"seeded table {table} has no stable primary key"
                raise RuntimeError(message)
            value = json.loads(str(raw))
            key = json.dumps([value[k] for k in keys], separators=(",", ":"))
            if typed:
                value[_TYPED] = dict(zip(names, values, strict=True))
            rows[key] = value
        if rows:
            result[table] = rows
    return result


def snapshot() -> tuple[Tables, set[tuple[str, str]]]:
    """Read a consistent, privileged snapshot without changing product state."""
    with (
        psycopg.connect(os.environ["UPGRADE_ADMIN_DATABASE_URL"]) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cursor.execute("SET LOCAL row_security=off")
        rows = read_rows(cursor, typed=True)
        cursor.execute("SELECT app,name FROM clinic_app.django_migrations")
        applied = {(str(app), str(name)) for app, name in cursor.fetchall()}
    return rows, applied


def catalog_counts(cursor: Cursor[tuple[object, ...]]) -> dict[str, int]:
    """Read the unfiltered catalog independently of the fixture row reader."""
    cursor.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='clinic_app' AND table_type='BASE TABLE'"
    )
    tables = [str(row[0]) for row in cursor.fetchall()]
    counts = {}
    for table in tables:
        cursor.execute(
            sql.SQL("SELECT count(*) FROM {}").format(
                sql.Identifier("clinic_app", table)
            )
        )
        value = cursor.fetchone()
        if value is None or not isinstance(value[0], int):
            message = f"cannot inventory fixture table {table}"
            raise RuntimeError(message)
        counts[table] = value[0]
    return counts


def fixture_coverage_failure(seeded: set[str], covered: set[str]) -> str | None:
    """Coverage must match the write set, including runtime side effects."""
    if seeded != covered:
        return f"sweep coverage mismatch: {sorted(seeded ^ covered)}"
    return None


def capture_base_counts() -> None:
    """Record counts only before seeding, including migration-owned tables."""
    with (
        psycopg.connect(os.environ["UPGRADE_ADMIN_DATABASE_URL"]) as admin,
        admin.cursor() as cursor,
    ):
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cursor.execute("SET LOCAL row_security=off")
        counts = catalog_counts(cursor)
    with Path(os.environ["UPGRADE_SEED_COUNTS"]).open("x", encoding="utf-8") as stream:
        json.dump(counts, stream, sort_keys=True)


def verify_seed_inventory() -> None:
    """Compare independent table inventories without capturing seeded values."""
    before = json.loads(
        Path(os.environ["UPGRADE_SEED_COUNTS"]).read_text(encoding="utf-8")
    )
    with (
        psycopg.connect(os.environ["UPGRADE_ADMIN_DATABASE_URL"]) as admin,
        admin.cursor() as cursor,
    ):
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cursor.execute("SET LOCAL row_security=off")
        after = catalog_counts(cursor)
        populated = set(read_rows(cursor))
    written = {table for table, count in after.items() if count != before.get(table)}
    if failure := fixture_coverage_failure(written, populated):
        raise RuntimeError(failure)
    with Path(os.environ["UPGRADE_SEED_WRITES"]).open("x", encoding="utf-8") as stream:
        json.dump(sorted(written), stream)


def differences(expected: Tables, actual: Tables) -> list[str]:
    """Report keys and columns only; never print mismatched values."""
    failures = []
    for table in sorted(expected.keys() | actual.keys()):
        rows = expected.get(table, {})
        observed = actual.get(table, {})
        for key in sorted(rows.keys() | observed.keys()):
            if key not in rows or key not in observed:
                failures.append(f"seeded value mismatch: {table}.<row> {key}")
                continue
            old, new = rows[key], observed[key]
            old_bytes = cast("dict[str, str]", old.get(_TYPED, {}))
            new_bytes = cast("dict[str, str]", new.get(_TYPED, {}))
            failures.extend(
                f"seeded value mismatch: {table}.{col} {key}"
                for col in sorted((old.keys() | new.keys()) - {_TYPED})
                if (
                    col not in old
                    or col not in new
                    or json.dumps(old[col], sort_keys=True)
                    != json.dumps(new[col], sort_keys=True)
                    or (col in old_bytes and old_bytes[col] != new_bytes.get(col))
                )
            )
    return failures
