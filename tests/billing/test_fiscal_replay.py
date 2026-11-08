"""Create-command replay must retain the requested clinic and fiscal book."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Never
from uuid import UUID

import pytest
from apps.billing import fiscal
from apps.billing.errors import BillingAccessDeniedError
from apps.billing.fiscal_policy import FiscalRefusedError
from apps.billing.models import (
    FiscalConfiguration,
    FiscalDocument,
    Invoice,
    JournalBook,
)

from billing.replay_support import QueryTrace, VisibleRows, refusal_shape

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass
class ReplayScope:
    book: JournalBook
    document: FiscalDocument
    trace: QueryTrace


@pytest.fixture
def replay_scope(monkeypatch: pytest.MonkeyPatch) -> ReplayScope:
    book = JournalBook(id=UUID(int=5801), clinic_id=UUID(int=5802))
    invoice_id, configuration_id = UUID(int=5803), UUID(int=5804)
    document = FiscalDocument(
        id=UUID(int=5805),
        clinic_id=book.clinic_id,
        book=book,
        invoice_id=invoice_id,
        configuration_id=configuration_id,
        create_command_id=UUID(int=5806),
        create_fingerprint=hashlib.sha256(
            b"clinic-fiscal-create-v1\0" + invoice_id.bytes + configuration_id.bytes
        ).digest(),
    )
    scope = ReplayScope(book=book, document=document, trace=[])

    @contextmanager
    def authorized_book(*, clinic_id: UUID, permission: str) -> Iterator[JournalBook]:
        assert clinic_id == scope.book.clinic_id
        assert permission == "settlement.post"
        yield scope.book

    def unavailable_configuration(**_selectors: object) -> Never:
        scope.trace.append(("configuration.get", tuple(sorted(_selectors))))
        raise FiscalConfiguration.DoesNotExist

    monkeypatch.setattr(fiscal, "book_write", authorized_book)
    monkeypatch.setattr(
        FiscalDocument.objects,
        "filter",
        VisibleRows((document,), scope.trace, "document").filter,
    )
    monkeypatch.setattr(FiscalConfiguration.objects, "get", unavailable_configuration)
    return scope


def assert_foreign_replay_denied_like_unknown(scope: ReplayScope) -> None:
    denials = []
    traces = []
    for command_id in (scope.document.create_command_id, UUID(int=5810)):
        scope.trace.clear()
        with pytest.raises(BillingAccessDeniedError) as denied:
            fiscal.prepare_fiscal_document(
                clinic_id=scope.book.clinic_id,
                invoice_id=scope.document.invoice_id,
                configuration_id=scope.document.configuration_id,
                command_id=command_id,
            )
        denials.append(refusal_shape(denied.value))
        traces.append(tuple(scope.trace))
    assert denials[0] == denials[1]
    assert traces[0] == traces[1]


def test_cross_clinic_replay_is_denied_like_unknown(
    replay_scope: ReplayScope,
) -> None:
    replay_scope.book = JournalBook(id=UUID(int=5807), clinic_id=UUID(int=5808))
    assert_foreign_replay_denied_like_unknown(replay_scope)


def test_cross_book_replay_in_the_same_clinic_is_denied_like_unknown(
    replay_scope: ReplayScope,
) -> None:
    replay_scope.book = JournalBook(
        id=UUID(int=5807), clinic_id=replay_scope.document.clinic_id
    )
    assert_foreign_replay_denied_like_unknown(replay_scope)


def test_same_scope_replay_returns_the_original(replay_scope: ReplayScope) -> None:
    original = replay_scope.document
    result = fiscal.prepare_fiscal_document(
        clinic_id=replay_scope.book.clinic_id,
        invoice_id=original.invoice_id,
        configuration_id=original.configuration_id,
        command_id=original.create_command_id,
    )
    assert result is original


def test_foreign_and_unknown_commands_share_the_real_local_review_refusal(
    replay_scope: ReplayScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    replay_scope.book = JournalBook(id=UUID(int=5807), clinic_id=UUID(int=5808))
    configuration = FiscalConfiguration(
        id=UUID(int=5812),
        clinic_id=replay_scope.book.clinic_id,
        book=replay_scope.book,
        review_kind="unreviewed",
    )
    invoice = Invoice(
        id=UUID(int=5811), clinic_id=replay_scope.book.clinic_id, state="open"
    )

    def local_configuration(**selectors: object) -> FiscalConfiguration:
        replay_scope.trace.append(("configuration.get", tuple(sorted(selectors))))
        assert selectors == {
            "pk": configuration.pk,
            "book": replay_scope.book,
            "clinic_id": replay_scope.book.clinic_id,
        }
        return configuration

    def local_invoice(**selectors: object) -> Invoice:
        replay_scope.trace.append(("invoice.get", tuple(sorted(selectors))))
        assert selectors == {
            "pk": invoice.pk,
            "clinic_id": replay_scope.book.clinic_id,
            "state__in": ("open", "paid"),
        }
        return invoice

    monkeypatch.setattr(FiscalConfiguration.objects, "get", local_configuration)
    monkeypatch.setattr(Invoice.objects, "get", local_invoice)
    denials = []
    traces = []
    for command_id in (replay_scope.document.create_command_id, UUID(int=5810)):
        replay_scope.trace.clear()
        with pytest.raises(FiscalRefusedError) as denied:
            fiscal.prepare_fiscal_document(
                clinic_id=replay_scope.book.clinic_id,
                invoice_id=invoice.pk,
                configuration_id=configuration.pk,
                command_id=command_id,
            )
        assert denied.value.code == "fiscal_configuration_unreviewed"
        denials.append(refusal_shape(denied.value))
        traces.append(tuple(replay_scope.trace))
    assert denials[0] == denials[1]
    assert traces[0] == traces[1]
