"""Exact interop posture; the migrated catalog must prove every declaration."""

from typing import Final

from apps.interop.migrations._import_sql import METADATA_COLUMNS

# Retained import facts carry bespoke clinic-authority policies (_import_sql.py).
CUSTOM_RLS_TABLES: Final = frozenset(
    {
        "interop_importmapping",
        "interop_importjob",
        "interop_importfile",
        "interop_importtransition",
        "interop_importbatch",
        "interop_importedrecord",
    }
)
# Hide markers hold only identifiers and must be readable by every tenant role:
# the restrictive intake visibility policies consult them for every reader.
RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset(
    {("interop_hiddenenrollment", "organization_id")}
)
NON_RLS_TABLES: Final[frozenset[str]] = frozenset()
RUNTIME_GRANTS: Final = {
    **{
        # Stored files and imported records: INSERT, and SELECT only on their
        # metadata columns below; payloads are resolver-only.
        table: frozenset(
            {"INSERT"} if table in METADATA_COLUMNS else {"SELECT", "INSERT"}
        )
        for table in CUSTOM_RLS_TABLES
    },
    # Only the rollback resolver writes hide markers.
    "interop_hiddenenrollment": frozenset({"SELECT"}),
}
COLUMN_GRANTS: Final = frozenset(
    {
        *(
            ("interop_importbatch", column, "UPDATE")
            for column in ("state", "committed_count", "committed_at")
        ),
        *(
            (table, column, "SELECT")
            for table, columns in METADATA_COLUMNS.items()
            for column in columns
        ),
    }
)
AGENT_GRANTS: Final[dict[str, frozenset[str]]] = {}
