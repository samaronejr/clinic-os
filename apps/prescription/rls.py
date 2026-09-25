"""Tenant posture declarations for the prescription domain.

Prescription tables carry bespoke draft/document/signature/verification
policies declared in ``apps/prescription/migrations/_draft_sql.py``,
``_document_sql.py``, ``_signature_sql.py`` and ``_verification_sql.py``;
none uses the shared ``tenant_isolation`` policy.
"""

from typing import Final

RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset()
CUSTOM_RLS_TABLES: Final[frozenset[str]] = frozenset(
    {
        "prescription_prescriptiondraft",
        "prescription_prescriptionitem",
        "prescription_prescriptiondocument",
        "prescription_signatureoperation",
        "prescription_signaturecallback",
        "prescription_prescriptiondocumentrelease",
        "prescription_prescriptiondocumentrevocation",
        "prescription_verificationprobe",
    }
)
NON_RLS_TABLES: Final[frozenset[str]] = frozenset()
RUNTIME_GRANTS: Final[dict[str, frozenset[str]]] = {
    "prescription_prescriptiondraft": frozenset({"SELECT", "INSERT"}),
    "prescription_prescriptionitem": frozenset({"SELECT", "INSERT"}),
    "prescription_prescriptiondocument": frozenset({"SELECT", "INSERT"}),
    "prescription_signatureoperation": frozenset({"SELECT", "INSERT"}),
    "prescription_signaturecallback": frozenset({"SELECT", "INSERT"}),
    "prescription_prescriptiondocumentrelease": frozenset({"SELECT", "INSERT"}),
    "prescription_prescriptiondocumentrevocation": frozenset({"SELECT", "INSERT"}),
    # Probe rows are written only by the resolver-owned verification
    # function; the runtime role holds no grants.
    "prescription_verificationprobe": frozenset(),
}
# Column-level privileges held by the runtime role (pg_attribute.attacl).
COLUMN_GRANTS: Final[frozenset[tuple[str, str, str]]] = frozenset(
    {
        ("prescription_prescriptiondocumentrelease", "revoked_at", "UPDATE"),
        ("prescription_prescriptiondocumentrelease", "revoked_by_id", "UPDATE"),
        ("prescription_prescriptiondraft", "state", "UPDATE"),
        ("prescription_prescriptiondraft", "updated_at", "UPDATE"),
        ("prescription_prescriptiondraft", "version", "UPDATE"),
        ("prescription_signatureoperation", "authorized_until", "UPDATE"),
        ("prescription_signatureoperation", "completed_at", "UPDATE"),
        ("prescription_signatureoperation", "evidence_id", "UPDATE"),
        ("prescription_signatureoperation", "evidence_snapshot", "UPDATE"),
        ("prescription_signatureoperation", "failure_reason", "UPDATE"),
        ("prescription_signatureoperation", "operation_id", "UPDATE"),
        ("prescription_signatureoperation", "signed_bytes", "UPDATE"),
        ("prescription_signatureoperation", "signed_digest", "UPDATE"),
        ("prescription_signatureoperation", "state", "UPDATE"),
    }
)
