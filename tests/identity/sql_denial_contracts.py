"""Exact denial contracts for executed SQL authority probes, from the live catalog.

The census never chooses how a denied PostgreSQL function answers. The shape
of each probed function is read from ``pg_proc`` (``proretset``,
``prorettype``, ``proargtypes`` and its OUT/TABLE columns, or the attributes of
a composite return type), and the exact denial follows from that shape:

* set-returning: exactly zero rows;
* scalar boolean: exactly ``[(False,)]``;
* any other scalar: exactly one row of NULLs, one per result column;
* a declared raise: exactly that exception type, SQLSTATE and primary message.

``CONTRACTS`` is the one declared table. It classifies every function the
census executes, and records the raise where a function refuses by raising
instead of answering with its sentinel. ``contract()`` fails when the function
is unclassified, when the catalog disagrees with the table, or when the
shape or a result column type is one this module cannot compare exactly.

``probe()`` is the single execution and comparison point. It returns an
issued ``SqlVerdict``, and the census harness decides only through
``redeem()``, which refuses any verdict ``probe()`` did not issue. An adapter
that computes its own allow/deny, or raises a Python denial instead, cannot
reach a census decision.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID
from weakref import WeakSet

import psycopg
from django.db import DatabaseError, connections, transaction
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
    "clinic_app.teleconsult_fail": Declared(Shape.BOOLEAN),
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


_ISSUED: WeakSet[SqlVerdict] = WeakSet()


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
    _ISSUED.add(verdict)
    return verdict


def redeem(verdict: object, function: str) -> bool:
    """The census decision for ``function``: only an issued, unredeemed verdict
    for that same function counts. Anything else is an adapter bypass."""
    bypass = (function, "not an issued contract verdict: bypassed probe()", verdict)
    assert type(verdict) is SqlVerdict, bypass
    assert verdict in _ISSUED, bypass
    _ISSUED.discard(verdict)
    assert verdict.function == function, (function, "verdict for", verdict.function)
    return verdict.allowed
