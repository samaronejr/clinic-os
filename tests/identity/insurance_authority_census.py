"""Execute insurance authority through retained source facts and real DB roles."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from pytest_django.fixtures import SettingsWrapper

from billing.journal_support import finance_setup
from insurance.cycle_support import build_cycle
from insurance.test_registered_booking import (
    insured,
    test_affirmative_checks_bind_the_real_appointment_before_confirmation,
    test_visible_foreign_payer_and_membership_cannot_be_used_in_booking,
)
from insurance.test_revenue_cycle import (
    test_duplicate_return_is_idempotent_and_cannot_post_cash,
    test_non_clinician_cannot_approve_new_appeal_assertions,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from identity.cut1_authority_census import ExecutedProofs
    from rbac_fixtures import RbacGraph


def exercise_insurance_authority(
    proofs: ExecutedProofs, graph: Callable[[], RbacGraph]
) -> None:
    """Issue certificates only for entrypoints reached by successful domain oracles."""
    settings = SettingsWrapper()
    settings.INSURANCE_SYNTHETIC_ENABLED = True
    try:

        def claims() -> None:
            setup = finance_setup._get_wrapped_function()(graph())
            cycle = build_cycle(setup)
            test_duplicate_return_is_idempotent_and_cannot_post_cash(cycle)
            test_non_clinician_cannot_approve_new_appeal_assertions(cycle)

        proofs.run("insurance source, replay and clinical approval authority", claims)
        for oracle in (
            test_visible_foreign_payer_and_membership_cannot_be_used_in_booking,
            test_affirmative_checks_bind_the_real_appointment_before_confirmation,
        ):
            setup = finance_setup._get_wrapped_function()(graph())
            subject = insured._get_wrapped_function()(setup, settings)
            proofs.run(oracle.__name__, partial(oracle, subject))
    finally:
        settings.finalize()
