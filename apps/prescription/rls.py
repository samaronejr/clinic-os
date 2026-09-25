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
