"""Todo 66 and 67 resolver pins from the migrated PostgreSQL 16 catalog.

Todo 67's values are derived from ``tests/interop/posture.json``, the live
snapshot of every ``interop_`` function and grant. Todo 66's import pins were
extracted from the same catalog and also cover the resolver grants its rollback
activity check needs on other apps' tables.
"""

import json
from pathlib import Path
from typing import Final, cast

IMPORT_TABLES: Final = (
    "interop_importmapping",
    "interop_importjob",
    "interop_importfile",
    "interop_importtransition",
    "interop_importbatch",
    "interop_importedrecord",
    "interop_hiddenenrollment",
)
# The rollback activity check reads three relations no earlier resolver did.
IMPORT_TABLE_GRANTS: Final = {
    *((table, "SELECT") for table in IMPORT_TABLES),
    ("interop_hiddenenrollment", "INSERT"),
    ("comms_participant", "SELECT"),
    ("intake_emergencycontact", "SELECT"),
    ("intake_patientaddress", "SELECT"),
}
IMPORT_RESULTS: Final = {
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
IMPORT_POSTURE: Final = {
    "interop_allowed": ("v", True, ["clinic_app", "clinic_resolver"]),
    "interop_record_visible": ("v", True, ["clinic_app", "clinic_resolver"]),
    "interop_hide_reversible": ("v", True, ["clinic_app", "clinic_resolver"]),
    "interop_guard": ("v", False, ["clinic_resolver"]),
    "interop_no_truncate": ("v", False, ["clinic_resolver"]),
}

CATALOG: Final = cast(
    "dict[str, list[list[object]]]",
    json.loads((Path(__file__).parents[1] / "interop" / "posture.json").read_text()),
)
TABLE_GRANTS: Final = IMPORT_TABLE_GRANTS | {
    (str(table), str(privilege))
    for grantee, table, privilege in CATALOG["table_grants"]
    if grantee == "clinic_resolver"
}
COLUMN_GRANTS: Final = {
    (str(table), str(column), str(privilege))
    for grantee, table, column, privilege in CATALOG["column_grants"]
    if grantee == "clinic_resolver"
}
RESULTS: Final = IMPORT_RESULTS | {
    (str(row[0]), str(row[1])): str(row[2]) for row in CATALOG["functions"]
}
POSTURE: Final = IMPORT_POSTURE | {
    str(row[0]): (str(row[3]), bool(row[8]), cast("list[str]", row[7]))
    for row in CATALOG["functions"]
}
INVOKERS: Final = {str(row[0]) for row in CATALOG["functions"] if not row[4]}
