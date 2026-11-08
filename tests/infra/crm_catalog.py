"""Todo 62 pins, extracted from the private PostgreSQL 16 catalog."""

from typing import Final

TABLE_GRANTS: Final = {
    (f"crm_{table}", "SELECT")
    for table in (
        "referralsource",
        "lead",
        "marketingpreference",
        "campaign",
        "campaignsend",
        "satisfactionsurvey",
        "publicbookingprofile",
    )
}
RESULTS: Final = {
    ("crm_contact_version", "channel_name text"): "integer",
    ("crm_guard", ""): "trigger",
    ("crm_immutable", ""): "trigger",
    ("crm_manager", "clinic uuid"): "boolean",
    ("crm_marketing_lock", ""): "trigger",
    ("crm_neutral_message", ""): "trigger",
    ("crm_patient_audit_scope", "record_id uuid, event_name text"): (
        "TABLE(session_id uuid, organization_id uuid, clinic_id uuid)"
    ),
    ("crm_public_profile", "requested_code text"): (
        "TABLE(clinic_id uuid, name text, description text)"
    ),
}
POSTURE: Final = {
    "crm_contact_version": (
        "s",
        True,
        ["clinic_app", "clinic_owner", "clinic_resolver"],
    ),
    "crm_manager": ("s", True, ["clinic_app", "clinic_owner", "clinic_resolver"]),
    "crm_public_profile": (
        "s",
        True,
        ["clinic_app", "clinic_owner", "clinic_resolver"],
    ),
    "crm_patient_audit_scope": ("s", False, ["clinic_owner", "clinic_resolver"]),
    **{
        name: ("v", False, ["clinic_resolver"])
        for name in (
            "crm_guard",
            "crm_immutable",
            "crm_marketing_lock",
            "crm_neutral_message",
        )
    },
}
