"""Bind Inventory's prepared role and invariant cases into the merged census."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from apps.identity.models import UserClinicRole

from inventory import test_authority_expiry, test_purchasing_equipment, test_stock

if TYPE_CHECKING:
    from collections.abc import Callable

    from identity.cut1_authority_census import ExecutedProofs
    from rbac_fixtures import RbacGraph


def run_inventory_census(
    proofs: ExecutedProofs, graph: Callable[[], RbacGraph]
) -> None:
    """Issue no certificates until the real prepared boundary oracle executes."""
    for role in UserClinicRole.Role.values:
        for oracle in (
            test_authority_expiry.test_use_is_limited_to_nurse_and_inventory_managers,
            test_authority_expiry.test_write_off_requires_inventory_manager_authority,
        ):
            proofs.run(
                f"inventory:{oracle.__name__}:{role}", partial(oracle, graph(), role)
            )
    for destination in ("same_org", "foreign_org", "unknown"):
        proofs.run(
            f"inventory transfer authority:{destination}",
            partial(
                test_authority_expiry.test_cross_clinic_transfer_without_org_permission_is_side_effect_free,
                graph(),
                destination,
            ),
        )
    for simple in (
        test_authority_expiry.test_revoked_manager_cannot_dispose_stock,
        test_authority_expiry.test_narrowed_inventory_permission_is_checked_on_each_use,
        test_authority_expiry.test_direct_write_off_cannot_bypass_nurse_authority,
        test_authority_expiry.test_expired_batch_is_alerted_and_cannot_be_used,
        test_stock.test_direct_runtime_debit_cannot_bypass_nonnegative_stock,
        test_purchasing_equipment.test_purchase_approval_and_receipt_are_separate_idempotent_commands,
        test_purchasing_equipment.test_replenishment_is_only_a_draft_within_remaining_approved_budget,
        test_purchasing_equipment.test_equipment_retains_calibration_and_maintenance_history,
    ):
        proofs.run(f"inventory:{simple.__name__}", partial(simple, graph()))
