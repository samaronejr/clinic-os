"""Exact interop posture; the migrated catalog must prove every declaration.

Governed-import tables (``migrations/_import_sql.py``), partner and export
tables (``migrations/_interop_sql.py``) and RNDS submissions
(``migrations/_rnds_sql.py``) carry bespoke FORCE-RLS policies; only the import
hide markers use the shared ``tenant_isolation`` policy. Partner secret digests
and access tokens are resolver-only: the runtime role holds no privilege on them
at all. RNDS submissions are resolver-written; the runtime only reads them under
``result.read``.
"""

from typing import Final

from apps.interop.migrations._import_sql import METADATA_COLUMNS

# Retained import facts carry bespoke clinic-authority policies (_import_sql.py).
IMPORT_RLS_TABLES: Final[frozenset[str]] = frozenset(
    {
        "interop_importmapping",
        "interop_importjob",
        "interop_importfile",
        "interop_importtransition",
        "interop_importbatch",
        "interop_importedrecord",
    }
)
# Partner and export records carry bespoke organization policies (_interop_sql.py).
PARTNER_RLS_TABLES: Final[frozenset[str]] = frozenset(
    {
        "interop_apiclient",
        "interop_apiclientsecret",
        "interop_apiaccesstoken",
        "interop_apiwritereceipt",
        "interop_webhooksubscription",
        "interop_webhookdelivery",
        "interop_webhookattempt",
        "interop_organizationexport",
        "interop_organizationexportpart",
    }
)
# RNDS submissions carry their own read policy and guard (_rnds_sql.py).
RNDS_RLS_TABLES: Final[frozenset[str]] = frozenset({"interop_rndssubmission"})
CUSTOM_RLS_TABLES: Final[frozenset[str]] = (
    IMPORT_RLS_TABLES | PARTNER_RLS_TABLES | RNDS_RLS_TABLES
)
# Hide markers hold only identifiers and must be readable by every tenant role:
# the restrictive intake visibility policies consult them for every reader.
RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset(
    {("interop_hiddenenrollment", "organization_id")}
)
NON_RLS_TABLES: Final[frozenset[str]] = frozenset()
RUNTIME_GRANTS: Final[dict[str, frozenset[str]]] = {
    **{
        # Stored files and imported records: INSERT, and SELECT only on their
        # metadata columns below; payloads are resolver-only.
        table: frozenset(
            {"INSERT"} if table in METADATA_COLUMNS else {"SELECT", "INSERT"}
        )
        for table in IMPORT_RLS_TABLES
    },
    # Only the rollback resolver writes hide markers.
    "interop_hiddenenrollment": frozenset({"SELECT"}),
    "interop_apiclient": frozenset({"SELECT"}),
    "interop_apiclientsecret": frozenset(),
    "interop_apiaccesstoken": frozenset(),
    "interop_apiwritereceipt": frozenset({"SELECT"}),
    "interop_webhooksubscription": frozenset({"SELECT"}),
    "interop_webhookdelivery": frozenset({"SELECT"}),
    "interop_webhookattempt": frozenset({"SELECT"}),
    "interop_organizationexport": frozenset({"SELECT"}),
    "interop_organizationexportpart": frozenset({"SELECT", "INSERT"}),
    "interop_rndssubmission": frozenset({"SELECT"}),
}
COLUMN_GRANTS: Final[frozenset[tuple[str, str, str]]] = frozenset(
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
        # The export worker advances its job under the requester's authority.
        *(
            ("interop_organizationexport", column, "UPDATE")
            for column in (
                "state",
                "revision",
                "part_count",
                "archive_bytes",
                "archive_sha256",
                "manifest_sha256",
                "dataset_count",
                "record_count",
                "attachment_count",
                "failure_reason",
            )
        ),
    }
)
AGENT_GRANTS: Final[dict[str, frozenset[str]]] = {}
