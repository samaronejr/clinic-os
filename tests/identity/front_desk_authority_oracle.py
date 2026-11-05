"""Execute the todo 54 front-desk witnesses inside the merged authority census."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from pytest_django.fixtures import SettingsWrapper

from ai import (
    test_front_desk,
    test_front_desk_guards,
    test_front_desk_portal,
    test_front_desk_views,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

    from identity.cut1_authority_census import ExecutedProofs
    from rbac_fixtures import RbacGraph


def run_front_desk_census(
    proofs: ExecutedProofs,
    graph: Callable[[], RbacGraph],
    patch: pytest.MonkeyPatch,
) -> None:
    """Require entry of every front-desk boundary under passing oracles.

    Signed provider deliveries drive the agent's principal-bound bridges and
    the channel session; the staff pages and services decide reviewed answers
    and handoffs on real roles; raw forgery shows the guards refuse alone.
    """
    settings = SettingsWrapper()
    try:
        for oracle in (
            test_front_desk.test_unverified_identity_cannot_learn_appointment_details,
            test_front_desk.test_booking_happens_only_after_explicit_acceptance_of_the_offer,
            test_front_desk.test_urgency_routes_to_a_human_with_fixed_safety_text,
            test_front_desk.test_missing_answer_says_it_does_not_know_and_forwards,
            test_front_desk_views.test_handoff_page_lists_reasons_and_resolves_by_post_body,
            test_front_desk_guards.test_front_desk_sql_rechecks_inactive_and_foreign_actor,
            test_front_desk_portal.test_verified_portal_books_only_after_accepting_the_exact_offer,
            test_front_desk_portal.test_portal_without_booking_scope_cannot_gain_it_through_the_agent,
        ):
            with patch.context() as scoped:
                proofs.run(oracle.__name__, partial(oracle, graph(), settings, scoped))
        proofs.run(
            "front desk reviewed answers page",
            partial(
                test_front_desk_views.test_knowledge_page_drafts_approves_and_retires_exact_versions,
                graph(),
            ),
        )
    finally:
        settings.finalize()
