"""Execute CRM boundaries through passing real-role, patient and public probes."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from pytest_django.fixtures import SettingsWrapper

from crm import test_authority, test_campaigns, test_migration_guard, test_surfaces
from crm.support import world

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

    from identity.cut1_authority_census import ExecutedProofs
    from rbac_fixtures import RbacGraph


def run_crm_census(
    proofs: ExecutedProofs,
    graph: Callable[[], RbacGraph],
    patch: pytest.MonkeyPatch,
) -> None:
    """Require entry to every discovered CRM boundary through its real contract."""
    settings = SettingsWrapper()
    try:
        with patch.context():
            proofs.run(
                "CRM consented neutral campaign",
                partial(
                    test_campaigns.test_synthetic_campaign_and_outbox_metadata_only,
                    world(graph(), settings),
                ),
            )
            for case in (
                test_surfaces.test_prospect_is_separate_encrypted_and_conversion_is_explicit,
                test_surfaces.test_manager_native_capture_preview_and_missing_consent_failure,
                test_surfaces.test_public_invitation_uses_fixed_url_and_generic_failures,
            ):
                proofs.run(case.__name__, partial(case, world(graph(), settings)))
            proofs.run(
                "CRM verified public booking",
                partial(
                    test_surfaces.test_public_booking_reuses_patient_slot_authority,
                    graph(),
                ),
            )
            proofs.run(
                "CRM all-role current manager authority",
                partial(
                    test_authority.test_manager_role_permissions_remain_current, graph()
                ),
            )
            proofs.run(
                "CRM immutable SQL receipt",
                partial(
                    test_authority.test_immutable_sql_receipts_cannot_be_truncated,
                    graph(),
                ),
            )
            proofs.run(
                "CRM raw diagnosis template refusal",
                partial(
                    test_authority.test_raw_marketing_template_rejected_by_sql,
                    graph(),
                    settings,
                ),
            )
            proofs.run(
                "CRM populated rollback refusal",
                partial(
                    test_migration_guard.test_populated_reverse_refuses_without_a_tenant_guc,
                    graph(),
                ),
            )
    finally:
        settings.finalize()
