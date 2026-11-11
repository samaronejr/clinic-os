"""Executed certificates for every discovered todo-65 Python authority boundary."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from identity import (
    test_break_glass,
    test_emergency_review_surface,
    test_emergency_views,
    test_enterprise_units,
    test_enterprise_views,
    test_scim,
    test_sso,
    test_support_access,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from identity.cut1_authority_census import ExecutedProofs
    from rbac_fixtures import RbacGraph


def run_enterprise_census(
    proofs: ExecutedProofs, graph: Callable[..., RbacGraph]
) -> None:
    """Observe real callable entry while the independent enterprise oracles run."""
    for case in (
        test_scim.test_provisioning_commits_audit_with_credential_principal,
        test_scim.test_approved_group_provisions_only_its_exact_unit_bundle,
        test_scim.test_directory_deactivation_revokes_managed_roles_and_existing_sessions,
        test_sso.test_approved_directory_subject_signs_in_without_a_factor_or_claim_elevation,
        test_break_glass.test_break_glass_commits_manager_and_patient_notices_review_and_safe_audit,
        test_emergency_review_surface.test_clinic_manager_can_read_reason_and_complete_only_an_ended_review,
        test_emergency_views.test_emergency_pages_grant_and_read_only_under_break_glass_authority,
        test_emergency_views.test_patient_access_log_lists_the_emergency_read_without_its_reason,
        test_support_access.test_support_is_masked_and_purpose_bound,
        test_enterprise_units.test_unit_revocation_is_idempotent_and_preserves_other_unit_memberships,
        test_enterprise_units.test_template_propagation_appends_a_version_and_replays_the_same_receipt,
        test_enterprise_views.test_governance_page_uses_native_private_forms,
    ):
        proofs.run(case.__name__, partial(case, graph()))
