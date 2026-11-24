"""Compile fixture definitions without running the native seed or reading rows."""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from uuid import UUID

import psycopg
from django.apps import apps
from django.db.migrations.state import ModelState
from psycopg import sql

from ops.testing.migration_upgrade_ai_recipe import declare_ai_rows
from ops.testing.migration_upgrade_inbox_fixture import CASES

if TYPE_CHECKING:
    from django.db.models import Model

_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
_UUID_VERSION = 4


@dataclass(frozen=True, slots=True)
class DeclaredRow:
    """One row's defined inputs, with unresolved symbolic relationships."""

    label: str
    pk: object
    fields: dict[str, object]


@dataclass(frozen=True, slots=True)
class DeclaredGraph:
    """Inputs and generated placeholders assigned by the pure recipe."""

    rows: list[DeclaredRow]
    uuids: set[UUID]
    anchors: list[datetime]
    core_keys: dict[str, UUID]


class _Intents:
    def __init__(self) -> None:
        self.rows: list[DeclaredRow] = []
        self.uuids: set[UUID] = set()
        self.anchors: list[datetime] = []
        self.clinic = self.uuid()
        self.owner = self.uuid()
        self.physician = self.uuid()
        self.patient = self.uuid()
        self.enrollment = self.uuid()

    def make(self, label: str, **fields: object) -> Model:
        identity = fields["id"] if "id" in fields else self.uuid()
        row = DeclaredRow(label, identity, fields)
        self.rows.append(row)
        return cast("Model", row)

    def find(self, label: str, **fields: object) -> Model:
        candidates = [
            row
            for row in self.rows
            if row.label == label
            and all(
                row.fields.get(key, self.clinic if key == "clinic_id" else None)
                == value
                for key, value in fields.items()
            )
        ]
        if len(candidates) != 1:
            message = f"fixture declaration must resolve exactly one {label}"
            raise RuntimeError(message)
        return cast("Model", candidates[0])

    def uuid(self) -> UUID:
        value = UUID(int=len(self.uuids) + 1, version=_UUID_VERSION)
        self.uuids.add(value)
        return value

    def now(self) -> datetime:
        # Different anchors remain distinguishable even after the declared
        # day offsets. They are not values to match in the live database.
        value = _EPOCH + timedelta(days=366 * (len(self.anchors) + 1))
        self.anchors.append(value)
        return value


def compile_ai_graph() -> DeclaredGraph:
    """Return the defined values and relationships of all direct AI writes."""
    backend = _Intents()
    appointment = backend.uuid()
    declare_ai_rows(
        backend,
        {
            "owner_id": str(backend.owner),
            "practitioner_id": str(backend.physician),
            "patient_id": str(backend.patient),
            "clinic_id": str(backend.clinic),
            "appointment_id": str(appointment),
        },
        backend.enrollment,
    )
    return DeclaredGraph(
        backend.rows,
        backend.uuids,
        backend.anchors,
        {
            "owner": backend.owner,
            "physician": backend.physician,
            "patient": backend.patient,
            "clinic": backend.clinic,
            "appointment": appointment,
            "enrollment": backend.enrollment,
        },
    )


def compile_ai_intent() -> list[DeclaredRow]:
    """Return the direct writes independently of the actual seed."""
    return compile_ai_graph().rows


def verify_ai_counts(*, with_inbox: bool) -> list[str]:
    """Check direct AI writes and declared shared-table fixture additions."""
    counts = Counter(
        ModelState.from_model(apps.get_model(row.label)).options.get(
            "db_table", row.label.replace(".", "_").lower()
        )
        for row in compile_ai_intent()
    )
    counts.update({"identity_user": 2, "identity_userclinicrole": 2})
    if with_inbox:
        counts.update(
            {
                "identity_user": 1,
                "identity_userclinicrole": 1,
                "comms_conversation": len(CASES),
                "comms_message": 2 * len(CASES),
                "comms_integrationoperation": sum(attempts for _, _, attempts in CASES),
            }
        )
    failures = []
    with (
        psycopg.connect(os.environ["UPGRADE_ADMIN_DATABASE_URL"]) as admin,
        admin.cursor() as cursor,
    ):
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        for table, expected in sorted(counts.items()):
            cursor.execute(
                sql.SQL("SELECT count(*) FROM {}").format(
                    sql.Identifier("clinic_app", table)
                )
            )
            actual = cursor.fetchone()
            if actual != (expected,):
                failures.append(
                    f"AI fixture row count: {table}; {expected=}; {actual=}"
                )
    return failures
