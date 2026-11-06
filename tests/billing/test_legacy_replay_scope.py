"""Legacy bridge preflight and imported rows stay in the requested clinic."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from apps.billing import compensation
from apps.billing.models import JournalBook, JournalSettlement

if TYPE_CHECKING:
    from collections.abc import Iterator

type QueryTrace = list[tuple[str, tuple[str, ...]]]


@dataclass(frozen=True)
class VisibleRows:
    rows: tuple[JournalSettlement, ...]
    trace: QueryTrace
    label: str

    def filter(self, **conditions: object) -> VisibleRows:
        self.trace.append((f"{self.label}.filter", tuple(sorted(conditions))))

        def matches(row: JournalSettlement) -> bool:
            for path, expected in conditions.items():
                value: object = row
                for field in path.split("__"):
                    value = getattr(value, field)
                if value != expected:
                    return False
            return True

        return VisibleRows(
            tuple(row for row in self.rows if matches(row)), self.trace, self.label
        )

    def exists(self) -> bool:
        self.trace.append((f"{self.label}.exists", ()))
        return bool(self.rows)


def test_legacy_import_preflight_uses_the_requested_clinic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace: QueryTrace = []
    existing = JournalSettlement(
        book=JournalBook(id=UUID(int=5840), clinic_id=UUID(int=5841)),
        legacy_settlement_id=UUID(int=5842),
    )

    class NoImportCursor:
        def execute(self, sql: str, params: object) -> None:
            trace.append(("cursor.execute", ()))

        def fetchone(self) -> None:
            return None

    @contextmanager
    def cursor() -> Iterator[NoImportCursor]:
        yield NoImportCursor()

    monkeypatch.setattr("apps.billing.compensation.connection.cursor", cursor)
    monkeypatch.setattr(
        JournalSettlement.objects,
        "filter",
        VisibleRows((existing,), trace, "legacy").filter,
    )
    compensation.bridge_legacy_settlement(
        clinic_id=UUID(int=5843), settlement_id=UUID(int=5842)
    )
    assert trace[:2] == [
        ("legacy.filter", ("book__clinic_id", "legacy_settlement_id")),
        ("legacy.exists", ()),
    ]


def test_legacy_import_fetch_uses_the_requested_clinic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clinic_id = UUID(int=5843)
    imported_id = UUID(int=5844)
    conditions_seen: dict[str, object] = {}

    class ImportCursor:
        result: tuple[UUID] | None = None

        def execute(self, sql: str, params: object) -> None:
            if "billing_import_legacy" in sql:
                self.result = (imported_id,)

        def fetchone(self) -> tuple[UUID] | None:
            return self.result

    @contextmanager
    def cursor() -> Iterator[ImportCursor]:
        yield ImportCursor()

    def get(**conditions: object) -> JournalSettlement:
        conditions_seen.update(conditions)
        raise JournalSettlement.DoesNotExist

    monkeypatch.setattr("apps.billing.compensation.connection.cursor", cursor)
    monkeypatch.setattr(
        JournalSettlement.objects, "filter", VisibleRows((), [], "legacy").filter
    )
    monkeypatch.setattr(JournalSettlement.objects, "get", get)
    with pytest.raises(JournalSettlement.DoesNotExist):
        compensation.bridge_legacy_settlement(
            clinic_id=clinic_id, settlement_id=UUID(int=5842)
        )
    assert conditions_seen == {"pk": imported_id, "book__clinic_id": clinic_id}
