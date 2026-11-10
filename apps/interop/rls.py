"""Exact FORCE-RLS posture of the interop domain.

Every interop table carries bespoke policies from
``apps/interop/migrations/_interop_sql.py``; none uses the shared
``tenant_isolation`` policy. Secret digests and access tokens are resolver-only:
the runtime role holds no privilege on them at all.
"""

from typing import Final

RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset()
CUSTOM_RLS_TABLES: Final[frozenset[str]] = frozenset(
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
NON_RLS_TABLES: Final[frozenset[str]] = frozenset()
RUNTIME_GRANTS: Final[dict[str, frozenset[str]]] = {
    "interop_apiclient": frozenset({"SELECT"}),
    "interop_apiclientsecret": frozenset(),
    "interop_apiaccesstoken": frozenset(),
    "interop_apiwritereceipt": frozenset({"SELECT"}),
    "interop_webhooksubscription": frozenset({"SELECT"}),
    "interop_webhookdelivery": frozenset({"SELECT"}),
    "interop_webhookattempt": frozenset({"SELECT"}),
    "interop_organizationexport": frozenset({"SELECT"}),
    "interop_organizationexportpart": frozenset({"SELECT", "INSERT"}),
}
# The export worker advances its job under the requester's current authority.
COLUMN_GRANTS: Final[frozenset[tuple[str, str, str]]] = frozenset(
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
)
