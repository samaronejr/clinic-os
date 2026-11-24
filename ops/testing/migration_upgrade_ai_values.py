"""Match the AI recipe's literal columns against native PostgreSQL rows."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import psycopg
import rfc8785
from django.apps import apps
from django.db.migrations.state import ModelState
from psycopg import sql
from psycopg.types.json import Json, Jsonb

from ops.testing.migration_upgrade_ai_intent import (
    DeclaredGraph,
    DeclaredRow,
    compile_ai_graph,
)
from ops.testing.migration_upgrade_core_fixture import (
    APPOINTMENT_START,
    CLINIC_NAME,
    ORGANIZATION_CNPJ,
    OWNER_USERNAME,
    PRACTITIONER_USERNAME,
)

if TYPE_CHECKING:
    from psycopg import Cursor

_UUID_VERSION = 4


@dataclass(frozen=True, slots=True)
class _Column:
    name: str
    db_type: str
    purpose: str | None
    codec: str


@dataclass(frozen=True, slots=True)
class _Context:
    graph: DeclaredGraph
    tokens: set[str]
    kek: str


@dataclass(frozen=True, slots=True)
class _Match:
    identity: object
    bindings: dict[UUID, UUID]


def _columns(
    cursor: Cursor[tuple[object, ...]], label: str
) -> tuple[str, dict[str, _Column]]:
    state = ModelState.from_model(apps.get_model(label))
    table = str(state.options.get("db_table", label.replace(".", "_").lower()))
    cursor.execute(
        "SELECT attname, pg_catalog.format_type(atttypid, atttypmod) "
        "FROM pg_catalog.pg_attribute "
        "WHERE attrelid=%s::regclass AND attnum > 0 AND NOT attisdropped",
        ["clinic_app." + table],
    )
    types = {str(name): str(db_type) for name, db_type in cursor.fetchall()}
    columns = {}
    for name, field in state.fields.items():
        field.set_attributes_from_name(name)
        attname, column = field.get_attname_column()
        if column not in types:
            continue
        metadata = _Column(
            column,
            types[column],
            getattr(field, "envelope_purpose", None),
            type(field).__name__,
        )
        columns[name] = metadata
        columns[attname] = metadata
    return table, columns


def _generated(value: object, graph: DeclaredGraph, tokens: set[str]) -> bool:
    if isinstance(value, DeclaredRow):
        return True
    if isinstance(value, UUID):
        return value in graph.uuids
    if isinstance(value, datetime):
        return any(
            value.year in (anchor.year - 1, anchor.year, anchor.year + 1)
            for anchor in graph.anchors
        )
    if isinstance(value, str):
        return any(token in value for token in tokens)
    items = (
        value.values()
        if isinstance(value, dict)
        else value
        if isinstance(value, (list, tuple))
        else ()
    )
    return any(_generated(item, graph, tokens) for item in items)


def _plain(value: object, column: _Column) -> bytes | None:
    if value is None:
        return None
    if column.codec == "EncryptedJSONField":
        return rfc8785.dumps(json.loads(json.dumps(value)))
    if isinstance(value, str):
        return value.encode("utf-8") if value else None
    if isinstance(value, date):
        return value.isoformat().encode("ascii")
    if isinstance(value, bytes):
        return value
    message = f"unknown fixture envelope representation: {column.name}"
    raise TypeError(message)


def _predicate(
    column: _Column, value: object, kek: str
) -> tuple[sql.Composable, list[Any]]:
    ident = sql.Identifier(column.name)
    if value is None:
        return sql.SQL("{} IS NULL").format(ident), []
    if column.purpose:
        plaintext = _plain(value, column)
        if plaintext is None:
            return sql.SQL("{} IS NULL").format(ident), []
        return (
            sql.SQL("clinic_app.tenant_decrypt(%s,%s,{0}) = %s AND {0} <> %s").format(
                ident
            ),
            [kek, column.purpose, plaintext, plaintext],
        )
    if column.db_type == "jsonb":
        value = Jsonb(value)
    elif column.db_type == "json":
        value = Json(value)
    # The type is from the declared model, not from fixture input values.
    return (
        sql.SQL(
            "pg_catalog.record_send(ROW({})) = pg_catalog.record_send(ROW(%s::{}))"
        ).format(ident, sql.SQL(column.db_type)),
        [value],
    )


def _uuid_ref(value: object, column: _Column, graph: DeclaredGraph) -> UUID | None:
    if isinstance(value, DeclaredRow):
        value = value.pk
    if isinstance(value, str) and column.db_type == "uuid":
        value = UUID(value)
    return value if isinstance(value, UUID) and value in graph.uuids else None


def _matches(
    refs: list[tuple[str, UUID]], data: list[tuple[object, ...]]
) -> list[_Match]:
    candidates = []
    for identity, *values in data:
        bindings: dict[UUID, UUID] = {}
        for (_, symbol), value in zip(refs, values, strict=True):
            if not isinstance(value, UUID) or value.version != _UUID_VERSION:
                break
            if bindings.setdefault(symbol, value) != value:
                break
        else:
            if len(set(bindings.values())) == len(bindings):
                candidates.append(_Match(identity, bindings))
    return candidates


def _candidates(
    cursor: Cursor[tuple[object, ...]],
    row: DeclaredRow,
    columns: dict[str, _Column],
    table: str,
    context: _Context,
) -> list[_Match]:
    predicates: list[sql.Composable] = []
    parameters: list[Any] = []
    refs: list[tuple[str, UUID]] = []
    if isinstance(row.pk, UUID):
        refs.append(("id", row.pk))
    for key, value in row.fields.items():
        column = columns[key]
        if _generated(value, context.graph, context.tokens):
            predicates.append(
                sql.SQL("{} IS NOT NULL").format(sql.Identifier(column.name))
            )
            if symbol := _uuid_ref(value, column, context.graph):
                refs.append((column.name, symbol))
            continue
        predicate, args = _predicate(column, value, context.kek)
        predicates.append(predicate)
        parameters.extend(args)
    condition = sql.SQL(" AND ").join(predicates) if predicates else sql.SQL("TRUE")
    fields = sql.SQL(", ").join(
        sql.Identifier(name) for name in ("id", *(name for name, _ in refs))
    )
    cursor.execute(
        sql.SQL("SELECT {} FROM {} WHERE {} ORDER BY id").format(
            fields,
            sql.Identifier("clinic_app", table),
            condition,
        ),
        parameters,
    )
    return _matches(refs, cursor.fetchall())


def _assign(candidates: list[set[object]]) -> bool:
    assigned: dict[object, int] = {}

    def augment(index: int, seen: set[object]) -> bool:
        for key in candidates[index] - seen:
            seen.add(key)
            if key not in assigned or augment(assigned[key], seen):
                assigned[key] = index
                return True
        return False

    return all(augment(index, set()) for index in range(len(candidates)))


def _connect(choices: list[tuple[str, list[_Match]]], known: dict[UUID, UUID]) -> bool:
    """Unify the UUID edges across all rows, preserving one-to-one identity."""
    used: set[tuple[str, object]] = set()
    inverse = {value: symbol for symbol, value in known.items()}

    def visit(pending: list[tuple[str, list[_Match]]]) -> bool:
        if not pending:
            return True
        ranked = []
        for table, matches in pending:
            viable = [
                match
                for match in matches
                if (table, match.identity) not in used
                and all(
                    known.get(symbol, value) == value
                    and inverse.get(value, symbol) == symbol
                    for symbol, value in match.bindings.items()
                )
            ]
            if not viable:
                return False
            ranked.append(viable)
        chosen = min(range(len(pending)), key=lambda index: len(ranked[index]))
        table = pending[chosen][0]
        rest = pending[:chosen] + pending[chosen + 1 :]
        for match in ranked[chosen]:
            key = (table, match.identity)
            fresh = match.bindings.keys() - known.keys()
            for symbol in fresh:
                value = match.bindings[symbol]
                known[symbol] = value
                inverse[value] = symbol
            used.add(key)
            if visit(rest):
                return True
            used.remove(key)
            for symbol in fresh:
                del inverse[known.pop(symbol)]
        return False

    return visit(choices)


def _core(cursor: Cursor[tuple[object, ...]], graph: DeclaredGraph) -> dict[UUID, UUID]:
    cursor.execute(
        """
        SELECT c.id,o.id,d.id,e.patient_id,e.id,a.id
        FROM clinic_app.identity_clinic c
        JOIN clinic_app.identity_user o ON o.username=%s
        JOIN clinic_app.identity_user d ON d.username=%s
        JOIN clinic_app.intake_patientclinicenrollment e ON e.clinic_id=c.id
        JOIN clinic_app.scheduling_appointment a ON a.clinic_id=c.id
             AND a.patient_id=e.patient_id AND a.practitioner_id=d.id
        WHERE c.organization_id=current_setting('app.current_tenant')::uuid
          AND c.name=%s AND a.start_at AT TIME ZONE c.timezone=%s::timestamp
        """,
        [OWNER_USERNAME, PRACTITIONER_USERNAME, CLINIC_NAME, APPOINTMENT_START],
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        return {}
    return {
        graph.core_keys[name]: value
        for name, value in zip(
            ("clinic", "owner", "physician", "patient", "enrollment", "appointment"),
            rows[0],
            strict=True,
        )
        if isinstance(value, UUID)
    }


def verify_ai_literal_values() -> list[str]:
    """Validate defined fields using one-to-one matching, without captured values."""
    graph = compile_ai_graph()
    tokens = {value.hex for value in graph.uuids} | {
        str(value) for value in graph.uuids
    }
    tokens.update(anchor.date().isoformat() for anchor in graph.anchors)
    kek = (
        (Path(os.environ["CLINIC_SECRET_DIR"]) / "tenant-kek.secret")
        .read_text(encoding="ascii")
        .strip()
    )
    context = _Context(graph, tokens, kek)
    by_label = defaultdict(list)
    for row in graph.rows:
        by_label[row.label].append(row)
    failures = []
    choices: list[tuple[str, list[_Match]]] = []
    with (
        psycopg.connect(os.environ["UPGRADE_ADMIN_DATABASE_URL"]) as admin,
        admin.cursor() as cursor,
    ):
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cursor.execute(
            "SELECT set_config('app.current_tenant',id::text,true) "
            "FROM clinic_app.identity_organization WHERE cnpj=%s",
            [ORGANIZATION_CNPJ],
        )
        if len(cursor.fetchall()) != 1:
            return ["AI literal fixture: organization natural key"]
        core = _core(cursor, graph)
        if len(core) != len(graph.core_keys):
            return ["AI literal fixture: core natural keys"]
        for label, rows in sorted(by_label.items()):
            table, columns = _columns(cursor, label)
            candidates = [
                _candidates(cursor, row, columns, table, context) for row in rows
            ]
            if not _assign([{match.identity for match in row} for row in candidates]):
                failures.append(f"AI literal fixture: no one-to-one match in {table}")
            choices.extend((table, row) for row in candidates)
    if not failures and not _connect(choices, core):
        failures.append("AI literal fixture: generated foreign-key graph mismatch")
    return failures
