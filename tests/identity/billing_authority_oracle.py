"""Execute journal authority witnesses before issuing merged census proofs."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from apps.identity.models import UserClinicRole

from billing import (
    test_authority_census,
    test_cash_and_authority,
    test_legacy_bridge,
    test_outgoing_sessions,
)
from billing.journal_support import finance_setup

if TYPE_CHECKING:
    from collections.abc import Callable

    from identity.cut1_authority_census import ExecutedProofs
    from rbac_fixtures import RbacGraph


def run_billing_census(proofs: ExecutedProofs, graph: Callable[[], RbacGraph]) -> None:
    """Exercise exact permission roots and stored session loss on real roles."""
    for role in UserClinicRole.Role.values:
        for state in test_authority_census.STATES:
            subject = finance_setup._get_wrapped_function()(graph())
            proofs.run(
                f"journal exact permission:{role}:{state}",
                partial(
                    test_authority_census.test_financial_permission_roots_use_current_exact_clinic_authority,
                    subject,
                    role,
                    state,
                ),
            )
    for oracle in (
        test_cash_and_authority.test_cash_close_has_zero_unexplained_balance_and_reopen_requires_persisted_step_up,
        test_outgoing_sessions.test_fresh_executor_can_use_frozen_instruction_after_old_session_revocation,
        test_legacy_bridge.test_first_legacy_receipt_prepares_missing_terms_and_commits_the_bridge,
    ):
        subject = finance_setup._get_wrapped_function()(graph())
        proofs.run(oracle.__name__, partial(oracle, subject))
