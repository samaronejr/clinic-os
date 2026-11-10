"""Executed certificates for every discovered todo-64 Python authority boundary."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from subscriptions import test_authority, test_contracts, test_state

if TYPE_CHECKING:
    from collections.abc import Callable

    from identity.cut1_authority_census import ExecutedProofs
    from rbac_fixtures import RbacGraph


def run_subscription_census(
    proofs: ExecutedProofs, graph: Callable[..., RbacGraph]
) -> None:
    """Observe real callable entry while the independent authority oracles run."""
    for case in (
        test_contracts.test_provisional_trial_is_bounded_and_keeps_clinical_core,
        test_contracts.test_org_admin_selects_packaging_but_cannot_write_entitlements,
        test_authority.test_sql_scope_and_commands_check_every_role_and_current_actor,
        test_authority.test_discovered_commercial_route_refuses_forged_entitlements_without_effects,
        test_authority.test_owner_cli_routes_synthetic_receipts_and_runtime_cannot_invoke_it,
        test_state.test_trial_active_failure_grace_restriction_cure_and_event_replay,
    ):
        proofs.run(case.__name__, partial(case, graph()))
