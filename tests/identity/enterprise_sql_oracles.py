"""Bind every enterprise SQL protocol to real-role behavior tests."""

from identity import (
    test_break_glass,
    test_break_glass_content,
    test_emergency_review_surface,
    test_enterprise_nonstaff,
    test_enterprise_scope,
    test_enterprise_units,
    test_scim,
    test_scim_bundle_issuer,
    test_sso,
    test_support_access,
)

ACTOR_PROTOCOL_ORACLES = {
    "clinic_app.enterprise_governance_guard()": (
        "identity.test_enterprise_inactive_actor."
        "test_support_scope_and_raw_guard_refuse_an_inactive_named_actor",
    ),
    "clinic_app.enterprise_sso_account(uuid,bytea)": (
        "identity.test_enterprise_inactive_actor."
        "test_sso_account_sql_refuses_its_inactive_directory_user",
    ),
    "clinic_app.enterprise_support_scope(uuid,uuid,bytea)": (
        "identity.test_enterprise_inactive_actor."
        "test_support_scope_and_raw_guard_refuse_an_inactive_named_actor",
    ),
    "clinic_app.enterprise_break_glass_usable(uuid,uuid)": (
        "identity.test_enterprise_inactive_actor."
        "test_emergency_and_task_sql_refuse_inactive_clinician_and_manager",
    ),
    "clinic_app.enterprise_finish_review(uuid)": (
        "identity.test_enterprise_inactive_actor."
        "test_emergency_and_task_sql_refuse_inactive_clinician_and_manager",
    ),
    "clinic_app.enterprise_scim_audit_scope(uuid)": (
        "identity.test_enterprise_inactive_actor."
        "test_scim_audit_scope_requires_absent_even_inactive_staff_actor",
    ),
    "clinic_app.enterprise_security_task(jsonb,text)": (
        "identity.test_enterprise_inactive_actor."
        "test_security_task_sql_rechecks_its_actual_manager",
    ),
}

SCIM_CASES = (
    test_scim.test_provisioning_commits_audit_with_credential_principal,
    test_scim.test_approved_group_provisions_only_its_exact_unit_bundle,
    test_scim.test_directory_deactivation_revokes_managed_roles_and_existing_sessions,
    test_scim.test_revoked_scim_token_is_unauthorized,
    test_scim.test_foreign_and_unknown_scim_resources_are_indistinguishable,
    test_scim.test_directory_resource_id_is_not_the_authentication_user_id,
    test_scim_bundle_issuer.test_modern_mapping_requires_owner_issuance_and_never_revokes_native_authority,
    test_sso.test_unmapped_subject_or_claim_cannot_elevate_a_role,
    test_sso.test_approved_directory_subject_signs_in_without_a_factor_or_claim_elevation,
    test_sso.test_oidc_state_is_single_use_and_bound_to_its_server_session,
    test_enterprise_scope.test_configuration_cannot_reuse_another_tenants_scope,
    test_enterprise_scope.test_browser_freshness_without_a_persisted_factor_is_not_authority,
    test_enterprise_scope.test_governance_obeys_actual_passkey_substitution_policy,
)
EMERGENCY_CASES = (
    test_break_glass.test_break_glass_without_reason_leaves_no_grant,
    test_break_glass.test_break_glass_rejects_unbounded_or_elapsed_expiry,
    test_break_glass.test_break_glass_commits_manager_and_patient_notices_review_and_safe_audit,
    test_break_glass.test_expired_break_glass_cannot_read_with_a_monkeypatched_clock,
    test_break_glass.test_break_glass_cannot_use_someone_elses_verified_session,
    test_break_glass_content.test_emergency_reader_returns_a_finalized_version_and_stops_on_revoke,
    test_emergency_review_surface.test_clinic_manager_can_read_reason_and_complete_only_an_ended_review,
)
SUPPORT_CASES = (
    test_support_access.test_support_is_masked_and_purpose_bound,
    test_support_access.test_jit_expiry_uses_a_clock_not_a_wait,
)
UNIT_CASES = (
    test_enterprise_units.test_unit_revocation_is_idempotent_and_preserves_other_unit_memberships,
    test_enterprise_units.test_template_propagation_appends_a_version_and_replays_the_same_receipt,
)
CONTEXT_CASES = (
    test_enterprise_nonstaff.test_scim_token_cannot_read_staff_patient_tables,
    test_enterprise_nonstaff.test_nonstaff_context_cannot_borrow_an_existing_staff_actor,
)
CASES = {
    case.__name__: case
    for case in (
        *SCIM_CASES,
        *EMERGENCY_CASES,
        *SUPPORT_CASES,
        *UNIT_CASES,
        *CONTEXT_CASES,
    )
}
SQL_PROTOCOL_ORACLES = {
    **{
        f"clinic_app.{name}": [case.__name__ for case in SCIM_CASES]
        for name in (
            "enterprise_org_admin",
            "enterprise_directory_authorized",
            "enterprise_scim_auth",
            "enterprise_connection_scope",
            "enterprise_new_directory_user",
            "enterprise_sync_directory",
            "enterprise_sso_account",
            "enterprise_sso_principal",
        )
    },
    "clinic_app.enterprise_scim_audit_scope": [
        test_scim.test_provisioning_commits_audit_with_credential_principal.__name__,
    ],
    **{
        f"clinic_app.{name}": [case.__name__ for case in EMERGENCY_CASES]
        for name in (
            "enterprise_manager",
            "enterprise_break_glass_usable",
            "enterprise_break_glass_versions",
            "enterprise_break_glass_observations",
            "enterprise_patient_access_log",
            "enterprise_governance_guard",
            "enterprise_break_glass_complete",
            "enterprise_security_task",
            "enterprise_create_review",
            "enterprise_finish_review",
        )
    },
    "clinic_app.enterprise_support_scope": [case.__name__ for case in SUPPORT_CASES],
    **{
        f"clinic_app.{name}": [case.__name__ for case in UNIT_CASES]
        for name in (
            "enterprise_unit_member_revoke",
            "enterprise_template_propagate",
            "enterprise_unit_denied",
            "enterprise_unit_membership_guard",
            "enterprise_unit_policy_apply",
        )
    },
    "clinic_app.enterprise_staff_context": [case.__name__ for case in CONTEXT_CASES],
}
