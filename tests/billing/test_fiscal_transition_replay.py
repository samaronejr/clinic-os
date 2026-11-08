"""Transition keys cannot reveal receipts belonging to another fiscal scope."""

from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from apps.billing import fiscal
from apps.billing.fiscal_policy import FiscalRefusedError
from apps.billing.models import (
    FiscalConfiguration,
    FiscalDocument,
    FiscalTransition,
    JournalBook,
)

from billing.replay_support import VisibleRows, refusal_shape
from billing.test_fiscal import NOW, review
from billing.test_fiscal_replay import ReplayScope, replay_scope

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

__all__ = ("replay_scope",)


@pytest.fixture
def rehearsal_scope(
    replay_scope: ReplayScope,
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
) -> ReplayScope:
    replay_scope.document.configuration = FiscalConfiguration(**asdict(review()))
    replay_scope.document.rehearsal = True
    replay_scope.document.status = "rejected"
    settings.BILLING_SYNTHETIC_NFSE = True
    monkeypatch.setattr("apps.billing.fiscal.timezone.now", lambda: NOW)
    monkeypatch.setattr(fiscal, "resolved_data_mode", lambda: "synthetic")

    def get_document(**selectors: object) -> FiscalDocument:
        row = (
            VisibleRows((replay_scope.document,), replay_scope.trace, "document")
            .filter(**selectors)
            .first()
        )
        if row is None:
            raise FiscalDocument.DoesNotExist
        return row

    monkeypatch.setattr(
        FiscalDocument.objects,
        "select_related",
        lambda *_fields: SimpleNamespace(get=get_document),
    )
    return replay_scope


@pytest.mark.parametrize("same_clinic", [False, True])
def test_foreign_transition_and_unknown_key_have_identical_denials(
    rehearsal_scope: ReplayScope,
    monkeypatch: pytest.MonkeyPatch,
    same_clinic: bool,
) -> None:
    document = rehearsal_scope.document
    foreign = FiscalDocument(
        id=UUID(int=5820),
        clinic_id=document.clinic_id if same_clinic else UUID(int=5821),
        book=JournalBook(id=UUID(int=5822)),
    )
    receipt = FiscalTransition(
        document=foreign, command_id=UUID(int=5823), to_status="cancelled"
    )
    monkeypatch.setattr(
        FiscalTransition.objects,
        "filter",
        VisibleRows((receipt,), rehearsal_scope.trace, "transition").filter,
    )
    denials = []
    traces = []
    for command_id in (receipt.command_id, UUID(int=5824)):
        rehearsal_scope.trace.clear()
        with pytest.raises(FiscalRefusedError) as denied:
            fiscal.cancel_fiscal_rehearsal(
                clinic_id=document.clinic_id,
                document_id=document.pk,
                command_id=command_id,
            )
        assert denied.value.code == "fiscal_transition_denied"
        denials.append(refusal_shape(denied.value))
        traces.append(tuple(rehearsal_scope.trace))
    assert denials[0] == denials[1]
    assert traces[0] == traces[1]


@pytest.mark.parametrize(
    ("target", "status"), [("cancelled", "cancelled"), ("submitted", "authorized")]
)
def test_same_scope_transition_replay_returns_the_original(
    rehearsal_scope: ReplayScope,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    status: str,
) -> None:
    document = rehearsal_scope.document
    document.status = status
    receipt = FiscalTransition(
        document=document, command_id=UUID(int=5823), to_status=target
    )
    monkeypatch.setattr(
        FiscalTransition.objects,
        "filter",
        VisibleRows((receipt,), rehearsal_scope.trace, "transition").filter,
    )
    action = (
        fiscal.cancel_fiscal_rehearsal
        if target == "cancelled"
        else fiscal.submit_fiscal_rehearsal
    )
    assert (
        action(
            clinic_id=document.clinic_id,
            document_id=document.pk,
            command_id=receipt.command_id,
        )
        is document
    )
