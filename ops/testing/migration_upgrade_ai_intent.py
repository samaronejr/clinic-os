"""Compile fixture definitions without running the native seed or reading rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from uuid import UUID

from ops.testing.migration_upgrade_ai_recipe import declare_ai_rows

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
        value = _EPOCH + timedelta(seconds=len(self.anchors) + 1)
        self.anchors.append(value)
        return value


def compile_ai_intent() -> list[DeclaredRow]:
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
    return backend.rows
