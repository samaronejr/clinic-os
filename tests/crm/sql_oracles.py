"""Executable CRM protocol coverage, consumed by the generated SQL census."""

from typing import Final

from crm import test_authority, test_campaigns, test_migration_guard, test_surfaces

CASES: Final = {
    fn.__name__: fn
    for fn in (
        test_authority.test_manager_role_permissions_remain_current,
        test_authority.test_immutable_sql_receipts_cannot_be_truncated,
        test_authority.test_raw_marketing_template_rejected_by_sql,
        test_campaigns.test_synthetic_campaign_and_outbox_metadata_only,
        test_surfaces.test_public_invitation_uses_fixed_url_and_generic_failures,
        test_surfaces.test_post_visit_score_only_for_own_completed_appointment,
        test_migration_guard.test_populated_reverse_refuses_without_a_tenant_guc,
    )
}
# Direct actor-GUC witnesses for tests/identity/test_sql_inactive_actor.py: each
# flips only identity_user.is_active and observes the SQL decision change.
ACTOR_PROTOCOL_ORACLES: Final = {
    "clinic_app.crm_manager(uuid)": (
        "crm.test_authority.test_manager_role_permissions_remain_current",
    ),
    "clinic_app.crm_guard()": (
        "crm.test_authority.test_raw_crm_guard_refuses_an_inactive_manager",
    ),
}
SQL_PROTOCOL_ORACLES: Final = {
    "clinic_app.crm_manager": ["test_manager_role_permissions_remain_current"],
    "clinic_app.crm_immutable": ["test_immutable_sql_receipts_cannot_be_truncated"],
    "clinic_app.crm_neutral_message": ["test_raw_marketing_template_rejected_by_sql"],
    "clinic_app.crm_public_profile": [
        "test_public_invitation_uses_fixed_url_and_generic_failures"
    ],
    **{
        f"clinic_app.{name}": [
            "test_synthetic_campaign_and_outbox_metadata_only",
            "test_post_visit_score_only_for_own_completed_appointment",
        ]
        for name in (
            "crm_contact_version",
            "crm_guard",
            "crm_marketing_lock",
            "crm_patient_audit",
            "crm_patient_audit_scope",
        )
    },
}
