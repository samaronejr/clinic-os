"""A historical approval is not a reusable live dispatch credential."""

from __future__ import annotations

from uuid import uuid4

import pytest
from apps.billing.payouts import approve_payout, request_payout, transition_payout
from apps.billing.security import finance_session, require_finance_step_up
from apps.identity.session_inventory import revoke_session
from apps.identity.stepup import StepUpRequired

from billing.journal_support import (
    FinanceSetup,
    allocated,
    finance_scope,
    finance_setup,
    fresh_finance_session,
    invoice_terms,
)
from billing.test_accounting_policy import balance

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("finance_setup",)


def test_fresh_executor_can_use_frozen_instruction_after_old_session_revocation(
    finance_setup: FinanceSetup,
) -> None:
    terms = invoice_terms(finance_setup)
    original_key = fresh_finance_session(finance_setup)
    with finance_scope(finance_setup):
        allocated(finance_setup, terms, 1000)
        payout = request_payout(
            clinic_id=finance_setup.clinic_id,
            practitioner_id=finance_setup.graph.physician,
            amount_minor=300,
            command_id=uuid4(),
        )
        with finance_session(
            clinic_id=finance_setup.clinic_id, session_key=original_key
        ):
            payout = approve_payout(
                clinic_id=finance_setup.clinic_id, payout_id=payout.pk
            )
        original = payout.approval_session_id
        assert original is not None
        revoke_session(session_record_id=original)
        with (
            pytest.raises(StepUpRequired),
            finance_session(
                clinic_id=finance_setup.clinic_id, session_key=original_key
            ),
        ):
            require_finance_step_up(clinic_id=finance_setup.clinic_id)
    # The helper creates a different device and verifies through the real form.
    renewed_key = fresh_finance_session(finance_setup)
    with (
        finance_scope(finance_setup),
        finance_session(clinic_id=finance_setup.clinic_id, session_key=renewed_key),
    ):
        payout = transition_payout(
            clinic_id=finance_setup.clinic_id, payout_id=payout.pk, next_state="sending"
        )
        assert payout.state == "sending"
        assert payout.approval_session_id == original
        payout = transition_payout(
            clinic_id=finance_setup.clinic_id,
            payout_id=payout.pk,
            next_state="confirmed",
            evidence_reference=uuid4(),
        )
        assert payout.state == "confirmed"
        assert balance(finance_setup.book_id, "bank") == 700
        assert balance(finance_setup.book_id, "practitioner_payable_in_transit") == 0
