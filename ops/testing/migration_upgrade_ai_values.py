"""Match the AI recipe's literal columns against native PostgreSQL rows."""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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
_SUBSECOND = timedelta(microseconds=999_999)

type _ClockWindows = dict[datetime, tuple[datetime, datetime]]


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
    symbols: dict[str, UUID]
    uuid_pattern: re.Pattern[str]


@dataclass(frozen=True, slots=True)
class _Match:
    identity: object
    bindings: dict[UUID, UUID]
    clocks: _ClockWindows


def _merge_clocks(left: _ClockWindows, right: _ClockWindows) -> _ClockWindows | None:
    windows = left.copy()
    for anchor, (start, end) in right.items():
        prior_start, prior_end = windows.get(anchor, (start, end))
        windows[anchor] = max(start, prior_start), min(end, prior_end)
    earliest = None
    for start, end in (windows[key] for key in sorted(windows)):
        earliest = start if earliest is None else max(earliest, start)
        if earliest > end:
            return None
    return windows


def _clock(
    graph: DeclaredGraph, symbol: datetime, actual: object
) -> _ClockWindows | None:
    if not isinstance(actual, datetime) or actual.tzinfo is None:
        return None
    anchor = min(graph.anchors, key=lambda point: abs(point - symbol))
    rounded = symbol.microsecond == 0
    if rounded and actual.microsecond:
        return None
    offset = symbol - (anchor.replace(microsecond=0) if rounded else anchor)
    start = actual - offset
    return {anchor: (start, start + _SUBSECOND if rounded else start)}


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


def _uuid_text(symbol: str, value: str, context: _Context) -> dict[UUID, UUID] | None:
    parts = context.uuid_pattern.split(symbol)
    pattern = "".join(
        (r"([0-9a-f-]{36})" if "-" in part else r"([0-9a-f]{32})")
        if index % 2
        else re.escape(part)
        for index, part in enumerate(parts)
    )
    match = re.fullmatch(pattern, value)
    if match is None:
        return None
    bindings: dict[UUID, UUID] = {}
    for text, actual_text in zip(parts[1::2], match.groups(), strict=True):
        try:
            actual = UUID(actual_text)
        except ValueError:
            return None
        if actual_text != (str(actual) if "-" in text else actual.hex):
            return None
        if bindings.setdefault(context.symbols[text], actual) != actual:
            return None
    return bindings


@dataclass(slots=True)
class _Bindings:
    context: _Context
    uuids: dict[UUID, UUID]
    clocks: _ClockWindows

    def _bind_uuids(self, pairs: dict[UUID, UUID]) -> bool:
        return all(
            value.version == _UUID_VERSION
            and self.uuids.setdefault(symbol, value) == value
            for symbol, value in pairs.items()
        )

    def _bind_clock(self, symbol: datetime, actual: object) -> bool:
        constraint = _clock(self.context.graph, symbol, actual)
        merged = None if constraint is None else _merge_clocks(self.clocks, constraint)
        if merged is None:
            return False
        self.clocks = merged
        return True

    def _bind_text(self, symbol: str, actual: str) -> bool:
        if self.context.uuid_pattern.search(symbol) is not None:
            pairs = _uuid_text(symbol, actual, self.context)
            return pairs is not None and self._bind_uuids(pairs)
        try:
            expected_time = datetime.fromisoformat(symbol)
        except ValueError:
            return symbol == actual
        if not _generated(expected_time, self.context.graph, self.context.tokens):
            return symbol == actual
        try:
            actual_time = datetime.fromisoformat(actual)
        except ValueError:
            return False
        return actual_time.strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        ) == actual and self._bind_clock(expected_time, actual_time)

    def compare(self, symbol: object, actual: object) -> bool:
        if isinstance(symbol, UUID) and isinstance(actual, UUID):
            return self._bind_uuids({symbol: actual})
        if isinstance(symbol, datetime):
            return self._bind_clock(symbol, actual)
        if isinstance(symbol, str) and isinstance(actual, str):
            return self._bind_text(symbol, actual)
        if isinstance(symbol, dict) and isinstance(actual, dict):
            return symbol.keys() == actual.keys() and all(
                self.compare(value, actual[key]) for key, value in symbol.items()
            )
        if isinstance(symbol, (list, tuple)) and isinstance(actual, list):
            return len(symbol) == len(actual) and all(
                self.compare(left, right)
                for left, right in zip(symbol, actual, strict=True)
            )
        return type(symbol) is type(actual) and symbol == actual


def _matches(
    refs: list[tuple[_Column | None, object]],
    data: list[tuple[object, ...]],
    context: _Context,
) -> list[_Match]:
    candidates = []
    for identity, *values in data:
        bindings = _Bindings(context, {}, {})
        if all(
            bindings.compare(symbol, _read_dynamic(value, column))
            for (column, symbol), value in zip(refs, values, strict=True)
        ) and len(set(bindings.uuids.values())) == len(bindings.uuids):
            candidates.append(_Match(identity, bindings.uuids, bindings.clocks))
    return candidates


def _read_dynamic(value: object, column: _Column | None) -> object:
    if column is None or not column.purpose or not isinstance(value, bytes):
        return value
    if column.codec == "EncryptedJSONField":
        decoded = json.loads(value)
        if rfc8785.dumps(decoded) != value:
            return None
        return decoded
    return value.decode("utf-8")


def _candidates(
    cursor: Cursor[tuple[object, ...]],
    row: DeclaredRow,
    columns: dict[str, _Column],
    table: str,
    context: _Context,
) -> list[_Match]:
    predicates: list[sql.Composable] = []
    parameters: list[Any] = []
    refs: list[tuple[_Column | None, object]] = []
    if isinstance(row.pk, UUID):
        refs.append((None, row.pk))
    for key, value in row.fields.items():
        column = columns[key]
        if _generated(value, context.graph, context.tokens):
            predicates.append(
                sql.SQL("{} IS NOT NULL").format(sql.Identifier(column.name))
            )
            refs.append((column, _uuid_ref(value, column, context.graph) or value))
            continue
        predicate, args = _predicate(column, value, context.kek)
        predicates.append(predicate)
        parameters.extend(args)
    condition = sql.SQL(" AND ").join(predicates) if predicates else sql.SQL("TRUE")
    selections: list[sql.Composable] = [sql.Identifier("id")]
    select_args: list[str] = []
    for selected, _ in refs:
        name = sql.Identifier(selected.name if selected else "id")
        if selected and selected.purpose:
            selections.append(
                sql.SQL("clinic_app.tenant_decrypt(%s,%s,{})").format(name)
            )
            select_args.extend((context.kek, selected.purpose))
        else:
            selections.append(name)
    cursor.execute(
        sql.SQL("SELECT {} FROM {} WHERE {} ORDER BY id").format(
            sql.SQL(", ").join(selections),
            sql.Identifier("clinic_app", table),
            condition,
        ),
        [*select_args, *parameters],
    )
    return _matches(refs, cursor.fetchall(), context)


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

    def visit(pending: list[tuple[str, list[_Match]]], clocks: _ClockWindows) -> bool:
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
            merged = _merge_clocks(clocks, match.clocks)
            if merged is None:
                continue
            key = (table, match.identity)
            fresh = match.bindings.keys() - known.keys()
            for symbol in fresh:
                value = match.bindings[symbol]
                known[symbol] = value
                inverse[value] = symbol
            used.add(key)
            if visit(rest, merged):
                return True
            used.remove(key)
            for symbol in fresh:
                del inverse[known.pop(symbol)]
        return False

    return visit(choices, {})


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
    symbols = {text: value for value in graph.uuids for text in (str(value), value.hex)}
    tokens = set(symbols)
    tokens.update(anchor.date().isoformat() for anchor in graph.anchors)
    kek = (
        (Path(os.environ["CLINIC_SECRET_DIR"]) / "tenant-kek.secret")
        .read_text(encoding="ascii")
        .strip()
    )
    pattern = re.compile("(" + "|".join(map(re.escape, sorted(symbols))) + ")")
    context = _Context(graph, tokens, kek, symbols, pattern)
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
