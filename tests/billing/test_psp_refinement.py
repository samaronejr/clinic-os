"""Unknown is missing status at a version, not a concrete contradiction."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from apps.billing.money_types import JournalConflictError
from apps.billing.psp_models import PspEvidence, PspReceipt
from apps.billing.psp_posting import apply_psp_evidence

from billing.journal_support import FinanceSetup, finance_scope, finance_setup
from billing.test_psp_adapter import PAID
from billing.test_psp_observations import _install_status
from billing.test_psp_review import prepared_payment

if TYPE_CHECKING:
    from apps.billing.psp_contracts import PaymentState
    from apps.billing.psp_models import PspIntent

__all__ = ("finance_setup", "prepared_payment")
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    ("previous", "version", "expected"),
    [
        ("unknown", 2, "verified"),
        ("unknown", 3, "superseded"),
        ("reversed", 2, "superseded"),
    ],
)
def test_unknown_is_refinable_only_at_its_sequence_not_across_a_reversal(
    prepared_payment: tuple[FinanceSetup, PspIntent],
    previous: PaymentState,
    version: int,
    expected: str,
) -> None:
    setup, intent = prepared_payment
    decision = _install_status(intent, replace(PAID, status=previous, version=version))
    assert decision == ("unknown" if previous == "unknown" else "quarantined")
    assert _install_status(intent, PAID) == expected
    with finance_scope(setup):
        settled = PspEvidence.objects.get(intent=intent, status="settled")
        if expected == "verified":
            first = apply_psp_evidence(
                clinic_id=setup.clinic_id, evidence_id=settled.pk
            )
            assert (
                apply_psp_evidence(clinic_id=setup.clinic_id, evidence_id=settled.pk)
                == first
            )
        else:
            with pytest.raises(JournalConflictError):
                apply_psp_evidence(clinic_id=setup.clinic_id, evidence_id=settled.pk)
        assert PspReceipt.objects.filter(intent=intent).count() == int(
            expected == "verified"
        )
        assert (
            PspEvidence.objects.get(intent=intent, status=previous).decision == decision
        )
