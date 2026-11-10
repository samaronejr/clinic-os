"""Independent todo-65 pins derived from the enterprise SQL and owner schedule."""

from typing import Final

TABLE_GRANTS: Final = {
    (table, "SELECT")
    for table in (
        "identity_ssoconnection",
        "identity_scimtoken",
        "identity_scimresource",
        "identity_scimrolemapping",
        "identity_scimmembership",
        "identity_scimrolebinding",
        "identity_breakglassgrant",
        "identity_supportaccessgrant",
        "identity_governancenotice",
        "identity_unitgovernancechange",
    )
} | {("workflows_task", "INSERT")}
COLUMN_GRANTS: Final = {
    (table, column, "UPDATE")
    for table, columns in (
        ("identity_scimtoken", ("window_start", "window_count")),
        (
            "workflows_task",
            (
                "owner_user_id",
                "owner_role",
                "state",
                "revision",
                "last_command_key",
                "last_command_digest",
                "completion_evidence",
                "updated_at",
            ),
        ),
        (
            "identity_breakglassgrant",
            ("review_task_id", "reviewed_at", "reviewed_by_id"),
        ),
    )
    for column in columns
}
RESULTS: Final = {
    ("enterprise_org_admin", "clinic uuid"): "boolean",
    ("enterprise_directory_authorized", "requested_connection uuid"): "boolean",
    ("enterprise_scim_auth", "requested_hash bytea"): (
        "TABLE(connection_id uuid, organization_id uuid, "
        "clinic_id uuid, limited boolean)"
    ),
    ("enterprise_connection_scope", "requested_clinic uuid"): (
        "TABLE(connection_id uuid, organization_id uuid)"
    ),
    ("enterprise_scim_audit_scope", "requested_clinic uuid"): (
        "TABLE(principal_id uuid, organization_id uuid)"
    ),
    (
        "enterprise_sso_principal",
        "requested_connection uuid, subject_hash bytea",
    ): "uuid",
    ("enterprise_sso_account", "requested_connection uuid, subject_hash bytea"): (
        "TABLE(user_id uuid, username text, encoded_password text, resource_id uuid)"
    ),
    ("enterprise_manager", "clinic uuid"): "boolean",
    ("enterprise_break_glass_usable", "requested_grant uuid, clinic uuid"): "boolean",
    ("enterprise_break_glass_versions", "requested_grant uuid"): (
        "SETOF ehr_clinicaldocumentversion"
    ),
    (
        "enterprise_support_scope",
        "requested_grant uuid, requested_clinic uuid, requested_session bytea",
    ): (
        "TABLE(organization_id uuid, user_id uuid, expires_at timestamp with time zone)"
    ),
    ("enterprise_break_glass_observations", "requested_grant uuid"): (
        "SETOF ehr_observation"
    ),
    ("enterprise_patient_access_log", "clinic uuid, page_number integer"): (
        "TABLE(notice_id uuid, occurred_at timestamp with time zone, "
        "clinician_label text, "
        "expires_at timestamp with time zone, reviewed boolean, clinic_timezone text)"
    ),
    ("enterprise_governance_guard", ""): "trigger",
    ("enterprise_break_glass_complete", ""): "trigger",
    ("enterprise_security_task", "candidate jsonb, operation text"): "boolean",
    ("enterprise_create_review", "requested_grant uuid"): "uuid",
    ("enterprise_finish_review", "requested_grant uuid"): "boolean",
    ("enterprise_unit_denied", "clinic uuid, target_user uuid"): "boolean",
    ("enterprise_unit_membership_guard", ""): "trigger",
}
NO_APP_EXECUTE: Final = {
    "enterprise_governance_guard",
    "enterprise_break_glass_complete",
    "enterprise_security_task",
    "enterprise_unit_denied",
    "enterprise_unit_membership_guard",
}
OWNER_EXECUTE: Final = {
    "enterprise_org_admin",
    "enterprise_directory_authorized",
    "enterprise_scim_audit_scope",
    "enterprise_unit_denied",
}
POSTURE: Final = {
    name: (
        "v",
        name not in NO_APP_EXECUTE,
        sorted(
            {
                "clinic_resolver",
                *(["clinic_app"] if name not in NO_APP_EXECUTE else []),
                *(["clinic_owner"] if name in OWNER_EXECUTE else []),
            }
        ),
    )
    for name, _arguments in RESULTS
}
