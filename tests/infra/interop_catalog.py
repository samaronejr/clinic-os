"""Todo 66 resolver pins, extracted from the fresh PostgreSQL 16 catalog."""

from typing import Final

TABLES: Final = (
    "interop_importmapping",
    "interop_importjob",
    "interop_importfile",
    "interop_importtransition",
    "interop_importbatch",
    "interop_importedrecord",
    "interop_hiddenenrollment",
)
# The rollback activity check reads three relations no earlier resolver did.
TABLE_GRANTS: Final = {
    *((table, "SELECT") for table in TABLES),
    ("interop_hiddenenrollment", "INSERT"),
    ("comms_participant", "SELECT"),
    ("intake_emergencycontact", "SELECT"),
    ("intake_patientaddress", "SELECT"),
}
COLUMN_GRANTS: Final[set[tuple[str, str, str]]] = set()
RESULTS: Final = {
    ("interop_allowed", "requested_clinic uuid"): "boolean",
    ("interop_attachment", "requested_clinic uuid, requested_record uuid"): (
        "TABLE(record_id uuid, job_id uuid, enrollment_id uuid, media_type text,"
        " sha256 text, data bytea)"
    ),
    ("interop_attachment_checks", "requested_job uuid, kek text"): (
        "TABLE(files integer, total_bytes bigint, mismatches integer)"
    ),
    ("interop_batch_scope", "requested_batch uuid"): (
        "TABLE(organization_id uuid, clinic_id uuid, actor_id uuid)"
    ),
    ("interop_guard", ""): "trigger",
    ("interop_hide_reversible", "requested_job uuid"): (
        "TABLE(imported_enrollment uuid, hidden boolean)"
    ),
    ("interop_history", "requested_clinic uuid, requested_enrollment uuid"): (
        "TABLE(record_id uuid, job_id uuid, kind text, row_number integer,"
        " original_author bytea, original_recorded_at timestamp with time zone,"
        " content bytea)"
    ),
    ("interop_no_truncate", ""): "trigger",
    (
        "interop_record_visible",
        "requested_job uuid, requested_clinic uuid, requested_enrollment uuid",
    ): "boolean",
    ("interop_source", "requested_job uuid"): "bytea",
}
POSTURE: Final = {
    "interop_allowed": ("v", True, ["clinic_app", "clinic_resolver"]),
    "interop_record_visible": ("v", True, ["clinic_app", "clinic_resolver"]),
    "interop_hide_reversible": ("v", True, ["clinic_app", "clinic_resolver"]),
    "interop_guard": ("v", False, ["clinic_resolver"]),
    "interop_no_truncate": ("v", False, ["clinic_resolver"]),
}
