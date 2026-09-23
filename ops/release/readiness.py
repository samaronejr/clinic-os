"""Validate accountable release-readiness evidence for Clinic OS.

``python -m ops.release.readiness check --mode synthetic|live`` evaluates the
closed approval-record schema described in
``docs/compliance/RELEASE-EVIDENCE.md`` against the evidence root named by
``CLINIC_RELEASE_EVIDENCE_ROOT``. Each record names its capability,
accountable owner, approval reference, scope, system/environment, issue and
review dates, a root-relative evidence path, that file's SHA-256 digest and a
synthetic flag. Records marked ``synthetic`` are never accepted for live
readiness, and neither are evidence bytes that match — or attest themselves —
the shipped synthetic fixture bundle, whatever the enclosing record claims.
A passing report is not legal review or permission to deploy.

Synthetic mode defaults to the checked-in ``synthetic-evidence`` bundle, which
exists only to exercise the validator; ``CLINIC_RELEASE_ID`` labels the
produced report and is required for live checks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, NamedTuple

SYSTEM_IDENTIFIER: Final = "clinic-os"
EVIDENCE_ROOT_ENV: Final = "CLINIC_RELEASE_EVIDENCE_ROOT"
RELEASE_ID_ENV: Final = "CLINIC_RELEASE_ID"
DEFAULT_SYNTHETIC_RELEASE_ID: Final = "synthetic"
RECORDS_DIRECTORY: Final = "records"
EVIDENCE_DIRECTORY: Final = "evidence"
RECORD_SUFFIX: Final = ".record.json"
SHA256_HEX_LENGTH: Final = 64
DISCLAIMER: Final = (
    "This report is a mechanical evidence check only; it is not legal review "
    "and it is not permission to deploy or to process live data."
)

# The closed record schema: exactly these keys, no more, no fewer.
RECORD_FIELDS: Final = frozenset(
    {
        "capability",
        "owner",
        "approval_reference",
        "scope",
        "system",
        "environment",
        "issued_at",
        "review_by",
        "evidence_path",
        "evidence_sha256",
        "synthetic",
    }
)

# Capabilities whose evidence file must be a JSON attestation carrying
# validated fields; see _attestation_findings.
ATTESTED_CAPABILITIES: Final = frozenset(
    {
        "dpo_designation",
        "dpo_public_contact",
        "incident_record_retention",
    }
)

MINIMUM_INCIDENT_RETENTION_YEARS: Final = 5
INCIDENT_RETENTION_ANCHOR: Final = "registration"

# Marker attestation carried by every shipped synthetic fixture evidence
# file; ops/release/synthetic_evidence.py emits the same value.
SYNTHETIC_FIXTURE_ATTESTATION: Final = "synthetic release-evidence fixture"

# Every prerequisite the live-data gate, the compliance templates, the
# integration register and the plan's task-44 additions reconcile to. The
# order is the report order.
REQUIRED_CAPABILITIES: Final = (
    # Live-data gate items (docs/compliance/LIVE-DATA-GATE.md).
    "hosted_ci",
    "hosted_pitr",
    "monitoring_delivery",
    "legal_approval",
    "privacy_approval",
    "incident_response",
    "credential_rotation",
    "retention_policy",
    # Compliance supporting records (privacy/DPA/ROPA/incident templates).
    "dpa",
    "ropa",
    # Plan task-44 additions.
    "dpo_designation",
    "dpo_public_contact",
    "incident_record_retention",
    # Task 6/43 encryption and transport evidence; backup encryption is not
    # a substitute for any of these.
    "data_at_rest",
    "tenant_key_management",
    "managed_secrets",
    "tls_transport",
    # Integration-register obligations reconciled for release.
    "physician_registration",
    "qualified_signing",
    "signature_verification",
    "video",
    "email",
    "sms",
    "whatsapp",
    "pix",
    "attachment_storage",
    "attachment_scanning",
    "pdf_rendering",
)

CAPABILITY_PURPOSE: Final = {
    "hosted_ci": "hosted CI/deploy/TLS evidence for the release environment",
    "hosted_pitr": "encrypted provider backup and point-in-time recovery",
    "monitoring_delivery": "monitoring and alert delivery to named recipients",
    "legal_approval": "legal approval for the release scope",
    "privacy_approval": "privacy approval covering the privacy review items",
    "incident_response": "approved incident-response plan and contacts",
    "credential_rotation": "credential rotation approval and procedure",
    "retention_policy": "retention/deletion approval for all data classes",
    "dpa": "signed data-processing agreement review",
    "ropa": "record of processing activities inventory",
    "dpo_designation": (
        "formal DPO designation naming the accountable person; the evidence "
        "attestation must carry formal_designation=true and a non-empty "
        "named_accountable_person"
    ),
    "dpo_public_contact": (
        "published and validated public DPO contact; the evidence attestation "
        "must carry published=true, validated=true and a non-empty "
        "public_contact"
    ),
    "incident_record_retention": (
        "retention of every recorded incident, notified or not, for at least "
        "five years measured from registration (longer when required); the "
        "evidence attestation must carry minimum_years>=5, "
        "measured_from='registration' and covers_unnotified=true"
    ),
    "data_at_rest": "AES-256 encryption of every storage surface at rest",
    "tenant_key_management": "tenant-scoped key management and rotation",
    "managed_secrets": "managed secret loading and rotation",
    "tls_transport": "TLS transport policy for external traffic",
    "physician_registration": "physician registration verification obligation",
    "qualified_signing": "qualified document signing obligation",
    "signature_verification": "independent signature verification obligation",
    "video": "video consultation provider obligation",
    "email": "email delivery provider obligation",
    "sms": "SMS delivery provider obligation",
    "whatsapp": "WhatsApp delivery provider obligation",
    "pix": "PIX charging and reconciliation provider obligation",
    "attachment_storage": "private attachment and document storage obligation",
    "attachment_scanning": "attachment malware scanning obligation",
    "pdf_rendering": "PDF rendering obligation",
}


class _Evaluation(NamedTuple):
    """Per-run evaluation context shared by every record check."""

    mode: str
    now: datetime
    fixture_digests: frozenset[str]


def default_synthetic_root() -> Path:
    """Return the checked-in synthetic evidence bundle location."""
    return Path(__file__).resolve().parent / "synthetic-evidence"


def _parse_timestamp(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp; naive or malformed values return None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _nonempty(value: object) -> bool:
    """Return True for a non-blank string field."""
    return isinstance(value, str) and bool(value.strip())


def _incident_retention_findings(payload: dict[object, object]) -> list[str]:
    """Validate the incident-record-retention attestation fields."""
    findings: list[str] = []
    minimum_years = payload.get("minimum_years")
    if (
        not isinstance(minimum_years, int)
        or isinstance(minimum_years, bool)
        or minimum_years < MINIMUM_INCIDENT_RETENTION_YEARS
    ):
        findings.append(
            "incident_record_retention: evidence minimum_years must be an "
            f"integer of at least {MINIMUM_INCIDENT_RETENTION_YEARS}"
        )
    if payload.get("measured_from") != INCIDENT_RETENTION_ANCHOR:
        findings.append(
            "incident_record_retention: evidence must measure retention "
            f"from {INCIDENT_RETENTION_ANCHOR!r}"
        )
    if payload.get("covers_unnotified") is not True:
        findings.append(
            "incident_record_retention: evidence must cover all recorded "
            "incidents, notified or not (covers_unnotified=true)"
        )
    return findings


def _dpo_designation_findings(payload: dict[object, object]) -> list[str]:
    """Validate the DPO-designation attestation fields."""
    findings: list[str] = []
    if payload.get("formal_designation") is not True:
        findings.append(
            "dpo_designation: evidence must record a formal designation "
            "(formal_designation=true)"
        )
    if not _nonempty(payload.get("named_accountable_person")):
        findings.append(
            "dpo_designation: evidence must name the accountable person "
            "(named_accountable_person)"
        )
    return findings


def _dpo_public_contact_findings(payload: dict[object, object]) -> list[str]:
    """Validate the public-DPO-contact attestation fields."""
    findings: list[str] = []
    if payload.get("published") is not True:
        findings.append(
            "dpo_public_contact: evidence must show the contact is "
            "published (published=true)"
        )
    if payload.get("validated") is not True:
        findings.append(
            "dpo_public_contact: evidence must show the contact was "
            "validated (validated=true)"
        )
    if not _nonempty(payload.get("public_contact")):
        findings.append(
            "dpo_public_contact: evidence must carry the public contact "
            "value (public_contact)"
        )
    return findings


def _attestation_findings(capability: str, evidence_file: Path) -> list[str]:
    """Validate the JSON attestation required of specific capabilities."""
    try:
        payload = json.loads(evidence_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [f"{capability}: evidence file is not a readable JSON object"]
    if not isinstance(payload, dict):
        return [f"{capability}: evidence file is not a readable JSON object"]
    if capability == "incident_record_retention":
        return _incident_retention_findings(payload)
    if capability == "dpo_designation":
        return _dpo_designation_findings(payload)
    if capability == "dpo_public_contact":
        return _dpo_public_contact_findings(payload)
    return []


def _field_findings(
    capability: object, record: dict[object, object], mode: str, now: datetime
) -> list[str]:
    """Validate the accountable fields, dates and mode binding of a record."""
    label = capability if isinstance(capability, str) else "record"
    findings: list[str] = [
        f"{label}: {field} must name an accountable value"
        for field in ("owner", "approval_reference", "scope")
        if not _nonempty(record[field])
    ]
    if record["system"] != SYSTEM_IDENTIFIER:
        findings.append(
            f"{label}: system {record['system']!r} does not match {SYSTEM_IDENTIFIER!r}"
        )
    if record["environment"] != mode:
        findings.append(
            f"{label}: environment {record['environment']!r} does not match "
            f"the {mode!r} environment under evaluation"
        )
    if not isinstance(record["synthetic"], bool):
        findings.append(f"{label}: synthetic must be a boolean")
    elif mode == "live" and record["synthetic"]:
        findings.append(
            f"{label}: synthetic evidence is never accepted for live readiness"
        )
    findings.extend(_date_findings(label, record, now))
    return findings


def _date_findings(
    label: str, record: dict[object, object], now: datetime
) -> list[str]:
    """Validate the issue and review timestamps of a record."""
    findings: list[str] = []
    issued_at = _parse_timestamp(record["issued_at"])
    if issued_at is None:
        findings.append(f"{label}: issued_at is not a timezone-aware timestamp")
    elif issued_at > now:
        findings.append(f"{label}: issued_at is in the future")
    review_by = _parse_timestamp(record["review_by"])
    if review_by is None:
        findings.append(f"{label}: review_by is not a timezone-aware timestamp")
    elif review_by <= now:
        findings.append(f"{label}: approval expired; review_by is not future")
    return findings


def _resolve_evidence(
    capability: str, evidence_path: object, root: Path
) -> tuple[Path | None, str | None]:
    """Resolve a root-relative evidence path or return the rejection reason."""
    label = capability
    if not isinstance(evidence_path, str) or not evidence_path:
        return None, f"{label}: evidence_path must be a non-empty string"
    relative = Path(evidence_path)
    if relative.is_absolute() or ".." in relative.parts:
        return None, (f"{label}: evidence_path must stay inside the evidence root")
    candidate = root / relative
    if not candidate.is_file():
        return None, f"{label}: evidence file {evidence_path!r} is missing"
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root.resolve()):
        return None, (
            f"{label}: evidence file {evidence_path!r} escapes the evidence root"
        )
    return resolved, None


def _fixture_digests() -> frozenset[str]:
    """Return the SHA-256 digest of every file in the shipped fixture bundle."""
    root = default_synthetic_root()
    if not root.is_dir():
        return frozenset()
    return frozenset(
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    )


def _live_fixture_findings(
    capability: str,
    evidence_file: Path,
    actual_digest: str,
    fixture_digests: frozenset[str],
) -> list[str]:
    """Reject shipped or self-marked synthetic fixture bytes in live checks."""
    if actual_digest in fixture_digests:
        return [
            f"{capability}: evidence bytes match the shipped synthetic "
            "fixture bundle; synthetic fixture evidence is never accepted "
            "for live readiness"
        ]
    try:
        payload = json.loads(evidence_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if (
        isinstance(payload, dict)
        and payload.get("attestation") == SYNTHETIC_FIXTURE_ATTESTATION
    ):
        return [
            f"{capability}: evidence attests itself a synthetic fixture; "
            "synthetic fixture evidence is never accepted for live readiness"
        ]
    return []


def _evidence_findings(
    capability: str,
    record: dict[object, object],
    root: Path,
    evaluation: _Evaluation,
) -> list[str]:
    """Validate the evidence path, digest and attestation of a record."""
    label = capability
    evidence_file, failure = _resolve_evidence(
        capability, record["evidence_path"], root
    )
    if failure is not None:
        return [failure]
    findings: list[str] = []
    digest = record["evidence_sha256"]
    if not isinstance(digest, str) or not (
        len(digest) == SHA256_HEX_LENGTH
        and all(char in "0123456789abcdef" for char in digest)
    ):
        findings.append(
            f"{label}: evidence_sha256 must be a lowercase SHA-256 hex digest"
        )
    elif evidence_file is not None:
        actual = hashlib.sha256(evidence_file.read_bytes()).hexdigest()
        if actual != digest:
            findings.append(
                f"{label}: evidence digest mismatch for {record['evidence_path']!r}"
            )
        elif evaluation.mode == "live":
            findings.extend(
                _live_fixture_findings(
                    capability,
                    evidence_file,
                    actual,
                    evaluation.fixture_digests,
                )
            )
    if evidence_file is not None and capability in ATTESTED_CAPABILITIES:
        findings.extend(_attestation_findings(capability, evidence_file))
    return findings


def _evaluate_record(
    path: Path,
    record: object,
    root: Path,
    evaluation: _Evaluation,
) -> list[str]:
    """Return every reason one record cannot satisfy its capability."""
    label = path.name
    if not isinstance(record, dict):
        return [f"{label}: record is not a JSON object"]
    capability = record.get("capability")
    if isinstance(capability, str):
        label = capability
    keys = set(record)
    if keys != RECORD_FIELDS:
        detail = []
        if missing := sorted(RECORD_FIELDS - keys):
            detail.append(f"missing fields {missing}")
        if extra := sorted(keys - RECORD_FIELDS):
            detail.append(f"unexpected fields {extra}")
        return [f"{label}: record violates the closed schema ({'; '.join(detail)})"]
    findings: list[str] = []
    if not isinstance(capability, str) or capability not in REQUIRED_CAPABILITIES:
        findings.append(f"{label}: unrecognized capability {capability!r}")
    elif path.name != f"{capability}{RECORD_SUFFIX}":
        findings.append(
            f"{capability}: record must be stored as "
            f"{RECORDS_DIRECTORY}/{capability}{RECORD_SUFFIX}"
        )
    findings.extend(
        _field_findings(capability, record, evaluation.mode, evaluation.now)
    )
    if isinstance(capability, str):
        findings.extend(_evidence_findings(capability, record, root, evaluation))
    return findings


def _load_records(root: Path) -> tuple[list[tuple[Path, object]], list[str]]:
    """Read every record file; unreadable JSON becomes a root-level error."""
    records_directory = root / RECORDS_DIRECTORY
    if not records_directory.is_dir():
        return [], [f"evidence root {root} has no {RECORDS_DIRECTORY}/ directory"]
    records: list[tuple[Path, object]] = []
    errors: list[str] = []
    for path in sorted(records_directory.glob(f"*{RECORD_SUFFIX}")):
        try:
            records.append((path, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError):
            errors.append(f"{path.name}: record is not readable JSON")
    return records, errors


def _evaluate(root: Path, mode: str, now: datetime) -> dict[str, Any]:
    """Evaluate one evidence root for one mode and return the report body."""
    records, errors = _load_records(root)
    evaluation = _Evaluation(
        mode=mode,
        now=now,
        fixture_digests=(_fixture_digests() if mode == "live" else frozenset()),
    )
    claimed: dict[str, int] = {}
    for _path, record in records:
        capability = record.get("capability") if isinstance(record, dict) else None
        if isinstance(capability, str):
            claimed[capability] = claimed.get(capability, 0) + 1
    for capability, count in sorted(claimed.items()):
        if count > 1:
            errors.append(f"{capability}: {count} records claim the same capability")
    findings_by_capability: dict[str, list[str]] = {
        capability: [] for capability in REQUIRED_CAPABILITIES
    }
    for path, record in records:
        findings = _evaluate_record(path, record, root, evaluation)
        capability = record.get("capability") if isinstance(record, dict) else None
        if isinstance(capability, str) and capability in REQUIRED_CAPABILITIES:
            findings_by_capability[capability].extend(findings)
        else:
            errors.extend(
                findings or [f"{path.name}: unrecognized capability {capability!r}"]
            )
    capabilities: dict[str, Any] = {}
    for capability in REQUIRED_CAPABILITIES:
        findings = findings_by_capability[capability]
        if not claimed.get(capability):
            findings = [*findings, "no approval record supplied"]
        capabilities[capability] = {
            "satisfied": not findings,
            "findings": findings,
        }
    missing = [
        capability
        for capability, status in capabilities.items()
        if not status["satisfied"]
    ]
    return {
        "mode": mode,
        "ready": not missing and not errors,
        "errors": errors,
        "missing": missing,
        "capabilities": capabilities,
    }


def evaluate_evidence(
    root: Path, mode: str, release_id: str, now: datetime | None = None
) -> dict[str, Any]:
    """Produce the readiness report for one evidence root.

    Synthetic-mode reports also embed the live-mode gap report so an
    agent-executed check always surfaces the complete live prerequisite list.
    """
    evaluated_at = now if now is not None else datetime.now(UTC)
    report = _evaluate(root, mode, evaluated_at)
    report.update(
        {
            "release_id": release_id,
            "evidence_root": str(root),
            "evaluated_at": evaluated_at.isoformat(),
            "required_capabilities": list(REQUIRED_CAPABILITIES),
            "disclaimer": DISCLAIMER,
        }
    )
    if mode == "synthetic":
        live = _evaluate(root, "live", evaluated_at)
        report["live_gap_report"] = {
            "ready": live["ready"],
            "errors": live["errors"],
            "missing": live["missing"],
            "capabilities": live["capabilities"],
        }
    return report


def _check(arguments: argparse.Namespace) -> int:
    """Run the ``check`` operation and print the JSON report."""
    mode: str = arguments.mode
    configured_root = os.environ.get(EVIDENCE_ROOT_ENV)
    if configured_root:
        root = Path(configured_root)
    elif mode == "synthetic":
        root = default_synthetic_root()
    else:
        sys.stderr.write(
            f"readiness: {EVIDENCE_ROOT_ENV} is required for live checks\n"
        )
        return 2
    if not root.is_dir():
        sys.stderr.write(f"readiness: evidence root {root} is not a directory\n")
        return 2
    release_id = os.environ.get(RELEASE_ID_ENV)
    if not release_id:
        if mode == "synthetic":
            release_id = DEFAULT_SYNTHETIC_RELEASE_ID
        else:
            sys.stderr.write(
                f"readiness: {RELEASE_ID_ENV} is required for live checks\n"
            )
            return 2
    report = evaluate_evidence(root, mode, release_id)
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0 if report["ready"] else 1


def main(argv: list[str] | None = None) -> int:
    """Dispatch the closed ``check`` grammar; usage failures exit 2."""
    parser = argparse.ArgumentParser(prog="ops.release.readiness")
    operations = parser.add_subparsers(dest="operation", required=True)
    check = operations.add_parser("check", help="evaluate release-readiness evidence")
    check.add_argument("--mode", choices=("synthetic", "live"), required=True)
    arguments = parser.parse_args(argv)
    if arguments.operation == "check":
        return _check(arguments)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
