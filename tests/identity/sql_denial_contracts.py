"""Exact denial contracts for executed SQL authority probes, from the live catalog.

The census never chooses how a denied PostgreSQL function answers. The shape
of each probed function is read from ``pg_proc`` (``proretset``,
``prorettype``, ``proargtypes`` and its OUT/TABLE columns, or the attributes of
a composite return type), and the exact denial follows from that shape:

* set-returning: exactly zero rows;
* scalar boolean: exactly ``[(False,)]``;
* any other scalar: exactly one row of NULLs, one per result column;
* a declared raise: exactly that exception type, SQLSTATE and primary message.

``CONTRACTS`` classifies every directly called function the
census executes, and records the raise where a function refuses by raising
instead of answering with its sentinel. ``contract()`` fails when the function
is unclassified, when the catalog disagrees with the table, or when the
shape or a result column type is one this module cannot compare exactly.

``probe()`` executes direct calls; ``probe_dml()`` executes declared real
INSERT trigger members from ``DML_CONTRACTS`` and verifies stored rows.
Both return an identity- and content-sealed ``SqlVerdict``. The census
harness decides only through ``redeem()``, which refuses any unissued,
mutated, replayed or differently bound verdict. An adapter
that computes its own allow/deny, or raises a Python denial instead, cannot
reach a census decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, cast
from uuid import UUID
from weakref import WeakKeyDictionary

import psycopg
from django.db import DatabaseError, connections, transaction
from django.db.models import Model
from psycopg import errors, sql

from identity.sql_probe_catalog import signature

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.backends.utils import CursorWrapper

type SqlCell = tuple[type[object], object]
type SqlRows = tuple[SqlCell, ...]
type SqlRowset = tuple[SqlRows, ...]
type SqlArgument = str | UUID | list[str] | int | None


class Shape(StrEnum):
    SET = "set"
    BOOLEAN = "boolean"
    SCALAR = "scalar"


@dataclass(frozen=True)
class Refusal:
    sqlstate: str
    message: str


@dataclass(frozen=True)
class DmlMember:
    """One real trigger's relation, operation and TG_TABLE_NAME branch."""

    function: str
    relation: str
    operation: str
    branch: str


# Each declaration is checked against a real enabled BEFORE ROW INSERT trigger.
# New domain members extend this table; they cannot mint their own verdicts.
DML_CONTRACTS: dict[DmlMember, Refusal] = {
    DmlMember(
        "clinic_app.teleconsult_binding_guard",
        "clinic_app.teleconsult_teleconsultdevicecheck",
        "INSERT",
        "teleconsult_teleconsultdevicecheck",
    ): Refusal("42501", "teleconsult participant authority required"),
    DmlMember(
        "clinic_app.teleconsult_binding_guard",
        "clinic_app.teleconsult_teleconsultevent",
        "INSERT",
        "teleconsult_teleconsultevent",
    ): Refusal("42501", "teleconsult participant authority required"),
    DmlMember(
        "clinic_app.consent_guard",
        "clinic_app.consent_consenttext",
        "INSERT",
        "consent_consenttext",
    ): Refusal("42501", "consent staff authority required"),
    DmlMember(
        "clinic_app.consent_guard",
        "clinic_app.consent_consentacceptance",
        "INSERT",
        "consent_consentacceptance",
    ): Refusal("42501", "patient consent authority required"),
    DmlMember(
        "clinic_app.consent_guard",
        "clinic_app.consent_consentrevocation",
        "INSERT",
        "consent_consentrevocation",
    ): Refusal("42501", "patient consent authority required"),
    DmlMember(
        "clinic_app.consent_guard",
        "clinic_app.consent_refusalrecord",
        "INSERT",
        "consent_refusalrecord",
    ): Refusal("42501", "patient consent authority required"),
    DmlMember(
        "clinic_app.consent_guard",
        "clinic_app.consent_noticeversion",
        "INSERT",
        "consent_noticeversion",
    ): Refusal("42501", "consent staff authority required"),
    DmlMember(
        "clinic_app.consent_guard",
        "clinic_app.consent_aiusedisclosure",
        "INSERT",
        "consent_aiusedisclosure",
    ): Refusal("42501", "consent staff authority required"),
    DmlMember(
        "clinic_app.consent_guard",
        "clinic_app.consent_participantacknowledgment",
        "INSERT",
        "consent_participantacknowledgment",
    ): Refusal("42501", "consent staff authority required"),
}


@dataclass(frozen=True)
class Declared:
    shape: Shape
    # Set only where the function refuses by raising instead of its sentinel.
    refusal: Refusal | None = None
    positive_count: bool = False


MANAGER_REFUSAL = Refusal("42501", "clinic manager authority is required")

# Every function the permission census executes. The shape is checked against
# pg_proc on every probe; a refusal is checked against the function definition
# by test_sql_probe_catalog and against the raised error on every probe.
CONTRACTS: dict[str, Declared] = {
    "clinic_app.auth_lookup": Declared(Shape.SET),
    "clinic_app.billing_payment_event_recorder": Declared(Shape.BOOLEAN),
    "clinic_app.billing_staff_invoice": Declared(Shape.BOOLEAN),
    "clinic_app.ehr_assigned": Declared(Shape.BOOLEAN),
    "clinic_app.ehr_care": Declared(Shape.BOOLEAN),
    "clinic_app.ehr_history_care": Declared(Shape.BOOLEAN),
    "clinic_app.ehr_version_scope": Declared(Shape.SCALAR),
    "clinic_app.has_permission": Declared(Shape.BOOLEAN),
    "clinic_app.list_active_clinic_physicians": Declared(Shape.SET),
    "clinic_app.load_current_user": Declared(Shape.SET),
    "clinic_app.patient_booking_practitioner": Declared(Shape.BOOLEAN),
    "clinic_app.patient_booking_slots": Declared(Shape.SET),
    "clinic_app.patient_registry_count": Declared(
        Shape.SCALAR, MANAGER_REFUSAL, positive_count=True
    ),
    "clinic_app.patient_registry_page": Declared(Shape.SET, MANAGER_REFUSAL),
    "clinic_app.questionnaire_completion": Declared(Shape.SET),
    "clinic_app.questionnaire_staff": Declared(Shape.BOOLEAN),
    "clinic_app.retention_author_label": Declared(Shape.SCALAR),
    "clinic_app.retention_care": Declared(Shape.BOOLEAN),
    "clinic_app.retention_care_patients": Declared(Shape.SET),
    "clinic_app.retention_record_scope": Declared(Shape.SET),
    "clinic_app.teleconsult_assigned": Declared(Shape.BOOLEAN),
    "clinic_app.teleconsult_clinician": Declared(Shape.BOOLEAN),
    "clinic_app.teleconsult_fail": Declared(Shape.BOOLEAN),
    "clinic_app.teleconsult_mode_transition_scope": Declared(Shape.SET),
    "clinic_app.teleconsult_room_state": Declared(Shape.SCALAR),
    "clinic_app.teleconsult_session_scope": Declared(Shape.SET),
    "clinic_app.user_has_org": Declared(Shape.BOOLEAN),
    "clinic_app.user_organizations": Declared(Shape.SET),
    "clinic_app.waitlist_staff": Declared(Shape.BOOLEAN),
}


def sql_rowset(rows: Sequence[tuple[object, ...]]) -> SqlRowset:
    """Tag every cell with its exact runtime type, so Python equality coercion
    (``1 == True``, ``date == datetime`` subclasses) cannot alias two results."""
    return tuple(tuple((type(cell), cell) for cell in row) for row in rows)


@dataclass(frozen=True)
class Contract:
    function: str
    shape: Shape
    columns: tuple[type[object], ...]
    arity: int
    refusal: Refusal | None = None
    argument_types: tuple[str, ...] = ()
    positive_count: bool = False

    @property
    def denial(self) -> SqlRowset | None:
        if self.refusal is not None:
            return None
        if self.shape is Shape.SET:
            return ()
        if self.shape is Shape.BOOLEAN:
            return sql_rowset([(False,)])
        return sql_rowset([(None,) * len(self.columns)])


def derive(function: str, cursor: CursorWrapper) -> Contract:
    found = signature(function, cursor)
    if found.returns_set:
        shape = Shape.SET
    elif found.boolean_result:
        shape = Shape.BOOLEAN
    else:
        shape = Shape.SCALAR
    return Contract(
        function,
        shape,
        found.columns,
        len(found.arguments),
        argument_types=found.arguments,
    )


def contract(function: str, cursor: CursorWrapper) -> Contract:
    declared = CONTRACTS.get(function)
    assert declared is not None, (function, "unclassified: declare it in CONTRACTS")
    derived = derive(function, cursor)
    assert derived.shape is declared.shape, (
        function,
        "catalog shape",
        derived.shape,
        "declared shape",
        declared.shape,
    )
    if declared.positive_count:
        assert derived.columns == (int,), (function, "catalog count type")
        assert derived.shape is Shape.SCALAR, (function, "catalog count shape")
    return replace(
        derived, refusal=declared.refusal, positive_count=declared.positive_count
    )


def _grant(contract: Contract, observed: SqlRowset) -> bool:
    typed = all(
        len(row) == len(contract.columns)
        and all(
            kind is type(None) or kind is column
            for (kind, _value), column in zip(row, contract.columns, strict=True)
        )
        for row in observed
    )
    if contract.shape is Shape.SET:
        return bool(observed) and typed
    if contract.shape is Shape.BOOLEAN:
        return observed == sql_rowset([(True,)])
    if contract.positive_count:
        if len(observed) != 1 or len(observed[0]) != 1:
            return False
        ((_kind, count),) = observed[0]
        return type(count) is int and count > 0
    return (
        len(observed) == 1
        and typed
        and any(value is not None for _kind, value in observed[0])
    )


def decide(contract: Contract, rows: Sequence[tuple[object, ...]]) -> bool:
    """The single rowset comparison: exactly the derived denial is a denial,
    a well-formed grant is an allow, and every other result fails the probe."""
    observed = sql_rowset(rows)
    if observed == contract.denial:
        return False
    assert _grant(contract, observed), (
        contract.function,
        "neither the exact denial nor a well-formed grant",
        contract.shape,
        contract.denial,
        rows,
    )
    return True


def refused(contract: Contract, error: BaseException) -> bool:
    refusal = contract.refusal
    cause = error.__cause__
    return (
        refusal is not None
        and isinstance(cause, psycopg.Error)
        and cause.sqlstate == refusal.sqlstate
        and type(cause) is errors.lookup(refusal.sqlstate)
        and cause.diag.message_primary == refusal.message
    )


@dataclass(frozen=True, eq=False)
class SqlVerdict:
    function: str
    allowed: bool
    observed: SqlRowset | Refusal
    member: DmlMember | None = None


type _Contents = tuple[
    str, bool, SqlRowset | tuple[str, str], tuple[str, str, str, str] | None
]


def _contents(verdict: SqlVerdict) -> _Contents:
    invalid = (verdict.function, "changed issued contents")
    assert type(verdict.function) is str, invalid
    assert type(verdict.allowed) is bool, invalid
    member = verdict.member
    assert member is None or type(member) is DmlMember, invalid
    identity = (
        None
        if member is None
        else (member.function, member.relation, member.operation, member.branch)
    )
    assert identity is None or all(type(field) is str for field in identity), invalid
    observed = verdict.observed
    captured: SqlRowset | tuple[str, str]
    if isinstance(observed, Refusal):
        assert type(observed) is Refusal, invalid
        captured = (observed.sqlstate, observed.message)
        assert all(type(field) is str for field in captured), invalid
    else:
        assert type(observed) is tuple, invalid
        rows: list[SqlRows] = []
        for row in observed:
            assert type(row) is tuple, invalid
            cells: list[SqlCell] = []
            for cell in row:
                assert type(cell) is tuple, invalid
                assert len(cell) == 2, invalid
                kind, value = cell
                assert kind is type(value), invalid
                snapshot: object
                # Store immutable numeric contents, not UUID equality/display
                # or a datetime's potentially mutable timezone object.
                if type(value) is UUID:
                    snapshot = value.int
                    assert type(snapshot) is int, invalid
                elif type(value) is datetime:
                    offset = value.utcoffset()
                    snapshot = (
                        value.year,
                        value.month,
                        value.day,
                        value.hour,
                        value.minute,
                        value.second,
                        value.microsecond,
                        value.fold,
                        None
                        if offset is None
                        else timedelta.__floordiv__(offset, timedelta(microseconds=1)),
                    )
                elif type(value) is date:
                    snapshot = (value.year, value.month, value.day)
                else:
                    assert (
                        value is None
                        or type(value) is bool
                        or type(value) is int
                        or type(value) is str
                    ), invalid
                    snapshot = value
                cells.append((kind, snapshot))
            rows.append(tuple(cells))
        captured = tuple(rows)
    return verdict.function, verdict.allowed, captured, identity


_ISSUED: WeakKeyDictionary[SqlVerdict, _Contents] = WeakKeyDictionary()


def probe(
    function: str, arguments: Sequence[SqlArgument], *, using: str = "default"
) -> SqlVerdict:
    schema, name = function.split(".")
    with connections[using].cursor() as cursor:
        found = contract(function, cursor)
        assert len(arguments) == found.arity, (function, "arity", len(arguments))
        statement = sql.SQL("SELECT * FROM {}.{}({})").format(
            sql.Identifier(schema),
            sql.Identifier(name),
            sql.SQL(", ").join(
                sql.SQL("{}::{}").format(
                    sql.Placeholder(), sql.Identifier(*argument.split("."))
                )
                for argument in found.argument_types
            ),
        )
        observed: SqlRowset | Refusal
        try:
            with transaction.atomic(using=using):
                cursor.execute(statement, list(arguments))
                rows = cursor.fetchall()
        except DatabaseError as error:
            if not refused(found, error):
                raise
            assert found.refusal is not None
            allowed, observed = False, found.refusal
        else:
            allowed, observed = decide(found, rows), sql_rowset(rows)
    verdict = SqlVerdict(function, allowed, observed)
    _ISSUED[verdict] = _contents(verdict)
    return verdict


def _dml_identity(
    cursor: CursorWrapper,
    member: DmlMember,
    row: Model,
    column: str,
    identity_type: type[UUID | int],
) -> UUID | int:
    """Reserve an auto identity through its real sequence, never fabricate one."""
    identity = cast("object", row.pk)
    if identity is None and identity_type is int:
        cursor.execute(
            "SELECT nextval(pg_get_serial_sequence(%s,%s))",
            [member.relation, column],
        )
        reserved = cursor.fetchone()
        assert reserved is not None
        identity = reserved[0]
        row.pk = identity
    assert type(identity) is identity_type, (member, "exact row identity required")
    return identity


def probe_dml(member: DmlMember, row: Model, *, using: str = "default") -> SqlVerdict:
    """Execute a fresh real INSERT and issue its stored-row or exact-raise verdict.

    This issuer owns execution. It accepts no callback, Boolean result or
    fabricated rowset. The branch is the trigger's actual TG_TABLE_NAME, and
    every successful witness is re-read from the declared qualified relation.
    """
    member = replace(member)
    refusal = DML_CONTRACTS.get(member)
    assert refusal is not None, (member, "unclassified DML member")
    assert member.operation == "INSERT", (member, "unsupported operation")
    function_schema, function_name = member.function.split(".")
    relation_schema, relation_name = member.relation.split(".")
    assert member.branch == relation_name, (member, "wrong branch")
    assert isinstance(row, Model), (member, "unsaved Model row required")
    assert row._meta.db_table == relation_name, (member, "wrong row relation")
    assert row._state.adding is True, (member, "fresh unsaved row required")
    primary_key = row._meta.pk
    assert primary_key is not None, (member, "primary key required")
    column = primary_key.column
    assert column is not None, (member, "primary-key column required")
    statement = sql.SQL("SELECT {} FROM {}.{} WHERE {} = %s").format(
        sql.Identifier(column),
        sql.Identifier(relation_schema),
        sql.Identifier(relation_name),
        sql.Identifier(column),
    )
    with connections[using].cursor() as cursor:
        cursor.execute(
            """
            SELECT t.tgtype, t.tgenabled, p.prorettype='pg_catalog.trigger'::regtype,
                   p.pronargs, current_schema(),
                   a.atttypid='pg_catalog.uuid'::regtype,
                   a.atttypid='pg_catalog.int8'::regtype
              FROM pg_catalog.pg_trigger t
              JOIN pg_catalog.pg_proc p ON p.oid=t.tgfoid
              JOIN pg_catalog.pg_namespace f ON f.oid=p.pronamespace
              JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
              JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
              JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid AND a.attname=%s
             WHERE f.nspname=%s AND p.proname=%s
               AND n.nspname=%s AND c.relname=%s
               AND NOT t.tgisinternal AND (t.tgtype & 7)=7
            """,
            [
                column,
                function_schema,
                function_name,
                relation_schema,
                relation_name,
            ],
        )
        catalog = cursor.fetchall()
        assert len(catalog) == 1, (
            member,
            "not exactly one live INSERT trigger",
            catalog,
        )
        ((_events, enabled, trigger, arity, schema, uuid_key, bigint_key),) = catalog
        assert (enabled, trigger, arity, schema) == (
            "O",
            True,
            0,
            relation_schema,
        ), (member, "live trigger/catalog mismatch", catalog)
        assert (uuid_key, bigint_key) in {(True, False), (False, True)}, (
            member,
            "unsupported primary-key catalog type",
        )
        identity_type = UUID if uuid_key else int
        identity = _dml_identity(cursor, member, row, column, identity_type)
        found = Contract(member.function, Shape.SET, (identity_type,), 0, refusal)
        cursor.execute(statement, [identity])
        assert cursor.fetchall() == [], (member, "fresh row identity required")
        observed: SqlRowset | Refusal
        try:
            with transaction.atomic(using=using):
                row.save(force_insert=True, using=using)
                cursor.execute(statement, [identity])
                rows = cursor.fetchall()
                assert sql_rowset(rows) == sql_rowset([(identity,)]), (
                    member,
                    "not the exact stored row",
                    rows,
                )
        except DatabaseError as error:
            if not refused(found, error):
                raise
            cause = error.__cause__
            assert isinstance(cause, psycopg.Error)
            raising_function = re.search(
                r"PL/pgSQL function (?:"
                + re.escape(function_schema)
                + r"\.)?"
                + re.escape(function_name)
                + r"\(\) line \d+ at RAISE(?:\n|$)",
                cause.diag.context or "",
            )
            if raising_function is None:
                raise
            allowed, observed = False, refusal
        else:
            allowed, observed = True, sql_rowset(rows)
    verdict = SqlVerdict(member.function, allowed, observed, member)
    _ISSUED[verdict] = _contents(verdict)
    return verdict


def redeem(verdict: object, function: str | DmlMember) -> bool:
    """The census decision for ``function``: only an issued, unredeemed verdict
    for that same function counts. Anything else is an adapter bypass."""
    bypass = (function, "not an issued contract verdict: bypassed probe()", verdict)
    assert type(verdict) is SqlVerdict, bypass
    assert verdict in _ISSUED, bypass
    sealed = _ISSUED.pop(verdict)
    assert _contents(verdict) == sealed, (function, "changed issued contents")
    expected = function.function if isinstance(function, DmlMember) else function
    assert verdict.function == expected, (function, "verdict for", verdict.function)
    member = function if isinstance(function, DmlMember) else None
    assert verdict.member == member, (function, "verdict for member", verdict.member)
    return sealed[1]
