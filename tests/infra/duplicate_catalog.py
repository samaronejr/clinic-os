"""Exact installed duplicate-review resolver and privilege expectations."""

from typing import Final

TABLE_GRANTS: Final = {
    (table, "SELECT")
    for table in (
        "intake_duplicatecandidate",
        "intake_mergedecision",
        "intake_patientalias",
        "intake_patientduplicateindex",
        "intake_patientmergetransfer",
    )
}
COLUMN_GRANTS: Final = {
    ("intake_duplicatecandidate", "state", "UPDATE"),
    ("intake_demographicscorrection", "demographics_id", "SELECT"),
    ("intake_demographicscorrection", "actor_id", "SELECT"),
}
RESULTS: Final = {
    ("intake_duplicate_detection_guard", ""): "trigger",
    ("intake_duplicate_transition_guard", ""): "trigger",
    ("intake_merge_authorized", "requested_clinic uuid, requested_session uuid"): (
        "boolean"
    ),
    ("intake_merge_decision_guard", ""): "trigger",
    (
        "intake_merge_mutation_allowed",
        "requested_clinic uuid, requested_patient uuid",
    ): "boolean",
    ("intake_merge_receipt_guard", ""): "trigger",
    ("intake_merge_transfer_guard", ""): "trigger",
    ("intake_patient_alias_guard", ""): "trigger",
    ("intake_survivor_write_guard", ""): "trigger",
    ("patient_alias_overview", ""): "timestamp with time zone",
    ("patient_alias_set", "requested_patient uuid"): (
        "TABLE(patient_id uuid, survivor_patient_id uuid, "
        "unified_at timestamp with time zone)"
    ),
}
READERS: Final = {
    "intake_merge_authorized",
    "intake_merge_mutation_allowed",
    "patient_alias_overview",
    "patient_alias_set",
}
POSTURE: Final = {
    name: (
        "s" if name in READERS else "v",
        name in READERS,
        ["clinic_app", "clinic_owner", "clinic_resolver"]
        if name in READERS
        else ["clinic_resolver"],
    )
    for name, _arguments in RESULTS
}
