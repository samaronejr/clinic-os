"""Independent exact insurance resolver and least-privilege catalog."""

from typing import Final

TABLES: Final = (
    "insurance_payer",
    "insurance_payercontract",
    "insurance_tussterm",
    "insurance_pricetable",
    "insurance_insurancemembership",
    "insurance_guide",
    "insurance_batch",
    "insurance_batchguide",
    "insurance_submission",
    "insurance_return",
    "insurance_glosa",
    "insurance_appeal",
    "insurance_clinicalapproval",
    "insurance_paymentmatch",
    "insurance_payerrequest",
    "insurance_commandreceipt",
)
TABLE_GRANTS: Final = {
    *((table, "SELECT") for table in TABLES),
    ("intake_insurancemembership", "SELECT"),
    ("insurance_commandreceipt", "INSERT"),
}
COLUMN_GRANTS: Final = {
    ("intake_insurancemembership", "payer_id", "UPDATE"),
    *(
        ("scheduling_appointment", field, "UPDATE")
        for field in (
            "eligibility_check_id",
            "authorization_check_id",
            "authorization_reference",
            "updated_at",
        )
    ),
}
RESULTS: Final = {
    ("insurance_guard", ""): "trigger",
    ("intake_membership_payer_guard", ""): "trigger",
    ("insurance_registration_guard", ""): "trigger",
    ("insurance_payer_check_applied", ""): "trigger",
    ("insurance_appointment_guard", ""): "trigger",
    (
        "insurance_link_registered_payer",
        "clinic uuid, membership uuid, payer uuid, kek text",
    ): "boolean",
    (
        "insurance_booking_coverage",
        "clinic uuid, patient uuid, membership uuid, contract uuid",
    ): (
        "TABLE(membership_id uuid, contract_id uuid, clinic_id uuid, patient_id uuid, "
        "payer_id uuid, contract_payer_id uuid, valid_from bytea, valid_to bytea, "
        "starts_on date, ends_on date, eligibility_supported boolean, "
        "authorization_supported boolean)"
    ),
    (
        "insurance_record_booking_coverage",
        "clinic uuid, patient uuid, membership uuid, contract uuid, "
        "appointment uuid, starts timestamp with time zone, "
        "ends timestamp with time zone, kek text",
    ): "uuid",
    (
        "insurance_guide_source",
        "clinic uuid, encounter uuid, invoice uuid, membership uuid",
    ): (
        "TABLE(organization_id uuid, physician_id uuid, service_type_id uuid, "
        "performed_on date, amount_minor bigint)"
    ),
}
POSTURE: Final = {
    "intake_membership_payer_guard": ("v", False, ["clinic_resolver"]),
    "insurance_registration_guard": ("v", False, ["clinic_resolver"]),
    "insurance_payer_check_applied": ("v", False, ["clinic_resolver"]),
    "insurance_appointment_guard": ("v", False, ["clinic_resolver"]),
    "insurance_link_registered_payer": ("v", True, ["clinic_app", "clinic_resolver"]),
    "insurance_booking_coverage": ("s", True, ["clinic_app", "clinic_resolver"]),
    "insurance_record_booking_coverage": ("v", True, ["clinic_app", "clinic_resolver"]),
    "insurance_guard": ("v", False, ["clinic_resolver"]),
    "insurance_guide_source": ("v", True, ["clinic_app", "clinic_resolver"]),
}
