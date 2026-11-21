"""SQL probes after a real fixture commits; owner authority is never used to probe."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
from apps.identity.models import Clinic, Organization, User, UserClinicRole
from django.db import transaction
from psycopg import sql

from identity.permission_support import owner_context

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class Witness:
    key: str
    value: object


@dataclass(frozen=True, slots=True)
class Operation:
    table: str
    verb: str
    statement: sql.SQL | sql.Composed
    args: tuple[object, ...] = ()


@dataclass(frozen=True, slots=True)
class Writes:
    column: str
    update: str = "ACL"
    delete: str = "ACL"
    insert: str = "ACL"


@dataclass(frozen=True, slots=True)
class Runtime:
    actor: UUID
    tenant: UUID

    def install(self, db: psycopg.Connection[tuple[object, ...]]) -> None:
        db.execute(
            """
            SELECT set_config('app.current_user_id', %s, true),
                   set_config('app.current_tenant', %s, true),
                   set_config('app.current_patient_session', '', true)
            """,
            [str(self.actor), str(self.tenant)],
        )
        assert db.execute(
            "SELECT current_user, session_user, current_setting('app.current_tenant'), "
            "current_setting('app.current_user_id'), "
            "current_setting('row_security')"
        ).fetchone() == (
            "clinic_app",
            "clinic_app",
            str(self.tenant),
            str(self.actor),
            "on",
        )
        assert db.execute(
            "SELECT clinic_app.user_has_org(%s)", [self.tenant]
        ).fetchone() == (True,)


def foreign_staff() -> Runtime:
    """Install an unrelated, active owner; existing fixtures can share actors."""
    tenant = uuid4()
    with transaction.atomic():
        actor = User.objects.create(username=f"synthetic-f2-{tenant.hex}")
        with owner_context(tenant):
            organization = Organization.objects.create(
                id=tenant,
                name="Synthetic isolation probe organization",
                cnpj=f"{int(tenant) % 10**14:014d}",
            )
            clinic = Clinic.objects.create(
                organization=organization,
                name="Synthetic isolation probe clinic",
                crm_uf="SP",
                timezone="America/Sao_Paulo",
            )
            UserClinicRole.objects.create(
                user=actor,
                organization=organization,
                clinic=clinic,
                role=UserClinicRole.Role.OWNER,
            )
    return Runtime(actor.pk, tenant)


@contextmanager
def runtime_connection(
    url: str, authority: Runtime
) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with (
        psycopg.connect(url, prepare_threshold=None) as db,
        db.transaction(force_rollback=True),
    ):
        authority.install(db)
        yield db


def assert_operation(
    db: psycopg.Connection[tuple[object, ...]],
    operation: Operation,
    expected: str,
) -> None:
    actual = observe_operation(db, operation)
    assert actual in expected.split(" | "), (
        f"{operation.table} {operation.verb}: expected {expected}, got {actual}"
    )


def observe_operation(
    db: psycopg.Connection[tuple[object, ...]],
    operation: Operation,
) -> str:
    """Always roll back mutations, including an unexpected successful write."""
    try:
        with db.transaction(force_rollback=True):
            cursor = db.execute(operation.statement, operation.args)
            if operation.verb == "SELECT":
                (count,) = cursor.fetchone() or (-1,)
            else:
                count = cursor.rowcount
    except psycopg.Error as exc:
        # Primary diagnostics distinguish an ACL failure from a WITH CHECK
        # rejection (both use 42501) and from an earlier write-scope guard.
        message = exc.diag.message_primary or ""
        if (
            exc.sqlstate == "42501"
            and message == f"permission denied for table {operation.table}"
        ):
            return "ACL"
        if exc.sqlstate == "42501" and message.startswith(
            "new row violates row-level security policy"
        ):
            return "WITH CHECK"
        return f"{exc.sqlstate}: {message}"
    return str(count)


def read_operation(table: str, row: Witness) -> Operation:
    return Operation(
        table,
        "SELECT",
        sql.SQL("SELECT count(*) FROM {} WHERE {} = %s").format(
            sql.Identifier("clinic_app", table), sql.Identifier(row.key)
        ),
        (row.value,),
    )


def assert_read(
    db: psycopg.Connection[tuple[object, ...]],
    table: str,
    row: Witness,
    expected: str,
) -> None:
    assert_operation(db, read_operation(table, row), expected)


def write_operations(
    table: str,
    row: Witness,
    writes: Writes,
    payload: str | None = None,
) -> tuple[tuple[Operation, str], ...]:
    relation = sql.Identifier("clinic_app", table)
    key = sql.Identifier(row.key)
    column = sql.Identifier(writes.column)
    if writes.insert == "ACL":
        add = sql.SQL("INSERT INTO {} DEFAULT VALUES").format(relation)
        args: tuple[object, ...] = ()
    else:
        assert payload is not None
        add = sql.SQL(
            "INSERT INTO {0} SELECT * FROM jsonb_populate_record(NULL::{0}, %s::jsonb)"
        ).format(relation)
        args = (payload,)
    return (
        (Operation(table, "INSERT", add, args), writes.insert),
        (
            Operation(
                table,
                "UPDATE",
                sql.SQL("UPDATE {} SET {} = {} WHERE {} = %s").format(
                    relation, column, column, key
                ),
                (row.value,),
            ),
            writes.update,
        ),
        (
            Operation(
                table,
                "DELETE",
                sql.SQL("DELETE FROM {} WHERE {} = %s").format(relation, key),
                (row.value,),
            ),
            writes.delete,
        ),
        (Operation(table, "TRUNCATE", sql.SQL("TRUNCATE {}").format(relation)), "ACL"),
    )


def assert_writes(
    db: psycopg.Connection[tuple[object, ...]],
    table: str,
    row: Witness,
    writes: Writes,
    payload: str | None = None,
) -> None:
    for operation, expected in write_operations(table, row, writes, payload):
        assert_operation(db, operation, expected)
