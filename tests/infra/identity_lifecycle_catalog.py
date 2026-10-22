"""Exact installed Task-15 resolver signatures and privilege expectations."""

from typing import Final

TABLES: Final = (
    "identity_authenticatorreset",
    "identity_authenticatorretirement",
    "identity_onboardingprogress",
    "identity_recoverybatch",
    "identity_recoverycode",
    "identity_sessionrecord",
    "identity_staffinvitation",
)
TABLE_GRANTS: Final = {
    (table, privilege)
    for table in TABLES
    for privilege in ("SELECT", "INSERT", "UPDATE")
}
COLUMN_GRANTS: Final = {
    (table, column, "SELECT")
    for table, columns in (
        (
            "otp_totp_totpdevice",
            ("id", "user_id", "confirmed", "last_t", "last_used_at"),
        ),
        ("otp_static_staticdevice", ("id", "user_id", "confirmed")),
    )
    for column in columns
}
RESULTS: Final = {
    ("identity_session_authenticator_verified", "requested_hash bytea"): "boolean",
    ("identity_passkey_registration_allowed", "requested_hash bytea"): "boolean",
    ("identity_passkey_revoke_sessions", ""): "trigger",
    (
        "identity_passkey_policy_set",
        "requested_clinic uuid, requested_enabled boolean, requested_session bytea",
    ): "integer",
    ("identity_passkey_lookup", "requested_credential bytea"): (
        "TABLE(passkey_id uuid, user_id uuid, organization_id uuid, clinic_id uuid, "
        "username text, encoded_password text)"
    ),
    (
        "identity_passkey_use",
        "requested_passkey uuid, requested_counter bigint",
    ): "boolean",
    (
        "identity_session_verify_passkey",
        "requested_hash bytea, requested_passkey uuid",
    ): "boolean",
    (
        "identity_passkey_session_verified",
        "requested_hash bytea, requested_passkey uuid, requested_age integer",
    ): "boolean",
    ("identity_invitation_user", "requested_clinic uuid, requested_user uuid"): "uuid",
    (
        "identity_invitation_begin",
        "requested_hash bytea, requested_username bytea, requested_password text, "
        "requested_device_key text",
    ): "integer",
    (
        "identity_invitation_finish",
        "requested_hash bytea, requested_username text",
    ): "uuid",
    ("identity_invitation_claim", "requested_hash bytea"): (
        "TABLE(invitation_id uuid, organization_id uuid, clinic_id uuid, "
        "user_id uuid, desired_username bytea, encoded_password text, "
        "device_id integer)"
    ),
    (
        "identity_offboarding_clinics",
        "requested_user uuid, requested_clinic uuid",
    ): "uuid[]",
    (
        "identity_offboarding_execute",
        "requested_user uuid, requested_clinic uuid",
    ): "boolean",
    ("identity_pending_authority_guard", ""): "trigger",
    ("identity_recovery_consume", "requested_hash bytea"): "boolean",
    (
        "identity_reset_execute",
        "requested_reset uuid, requested_session bytea",
    ): "boolean",
    ("identity_reset_notification_clock", ""): "trigger",
    ("identity_reset_recipient", "requested_reset uuid"): "text",
    ("identity_reset_target", "requested_clinic uuid, requested_user uuid"): "boolean",
    ("identity_retire_authenticators", "requested_user uuid"): "void",
    ("identity_revoke_changed_sessions", ""): "trigger",
    (
        "identity_session_active",
        "requested_user uuid, requested_hash bytea",
    ): "boolean",
    (
        "identity_session_register",
        "requested_user uuid, requested_hash bytea, requested_network text, "
        "requested_family text, requested_expiry timestamp with time zone",
    ): "uuid",
    ("identity_session_revoke_hash", "requested_hash bytea"): "boolean",
    ("identity_session_revoked_cookie", "requested_hash bytea"): "boolean",
    ("identity_session_rotate_hash", "requested_hash bytea"): "boolean",
    (
        "identity_session_step_up",
        "requested_hash bytea, requested_clinic uuid",
    ): "boolean",
    ("identity_session_verified", "requested_hash bytea"): "boolean",
    (
        "identity_session_verify",
        "requested_hash bytea, requested_device integer",
    ): "boolean",
    ("identity_staff_directory", "requested_clinic uuid"): (
        "TABLE(user_id uuid, display_label text, bundle text)"
    ),
}
NO_APP_EXECUTE: Final = {
    "identity_passkey_revoke_sessions",
    "identity_invitation_user",
    "identity_invitation_begin",
    "identity_invitation_finish",
    "identity_offboarding_execute",
    "identity_pending_authority_guard",
    "identity_retire_authenticators",
    "identity_reset_notification_clock",
    "identity_revoke_changed_sessions",
}
STABLE: Final = {
    "identity_reset_recipient",
    "identity_reset_target",
    "identity_session_active",
    "identity_session_revoked_cookie",
    "identity_session_step_up",
    "identity_session_verified",
    "identity_staff_directory",
}
OWNER_EXECUTE: Final = {
    "identity_offboarding_clinics",
    "identity_pending_authority_guard",
    "identity_retire_authenticators",
}
POSTURE: Final = {
    name: (
        "s" if name in STABLE else "v",
        name not in NO_APP_EXECUTE,
        sorted(
            [
                "clinic_resolver",
                *(["clinic_app"] if name not in NO_APP_EXECUTE else []),
                *(["clinic_owner"] if name in OWNER_EXECUTE else []),
            ]
        ),
    )
    for name, _signature in RESULTS
}
