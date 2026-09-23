"""Build the checked-in synthetic release-evidence bundle.

The bundle under ``ops/release/synthetic-evidence/`` exists only so
``python -m ops.release.readiness check --mode synthetic`` exercises the full
validator against a complete, internally consistent record set. Every record
carries ``"synthetic": true``; the bundle is not approval evidence and is
never accepted for live readiness.

Regenerate after changing the schema or capability set::

    uv run --frozen --no-sync --no-env-file python -m ops.release.synthetic_evidence
"""

from __future__ import annotations

import hashlib
import json
import sys
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from pathlib import Path

from ops.release.readiness import (
    EVIDENCE_DIRECTORY,
    INCIDENT_RETENTION_ANCHOR,
    MINIMUM_INCIDENT_RETENTION_YEARS,
    RECORD_SUFFIX,
    RECORDS_DIRECTORY,
    REQUIRED_CAPABILITIES,
    SYNTHETIC_FIXTURE_ATTESTATION,
    SYSTEM_IDENTIFIER,
    default_synthetic_root,
)

SYNTHETIC_ISSUED_AT: Final = "2026-09-12T00:00:00+00:00"
# Synthetic fixtures carry a far-future review date; they are not real
# approvals and nothing about them expires into a live authorization.
SYNTHETIC_REVIEW_BY: Final = "2099-12-31T00:00:00+00:00"
SYNTHETIC_OWNER: Final = "synthetic-fixture (no accountable owner)"
SYNTHETIC_APPROVAL: Final = "synthetic-fixture (no approval granted)"
SYNTHETIC_ENVIRONMENT: Final = "synthetic"

_ATTESTATION_FIELDS: Final = {
    "dpo_designation": {
        "formal_designation": True,
        "named_accountable_person": "Synthetic DPO Fixture",
    },
    "dpo_public_contact": {
        "published": True,
        "validated": True,
        "public_contact": "dpo@synthetic.example.invalid",
    },
    "incident_record_retention": {
        "minimum_years": MINIMUM_INCIDENT_RETENTION_YEARS,
        "measured_from": INCIDENT_RETENTION_ANCHOR,
        "covers_unnotified": True,
    },
}


def _evidence_payload(capability: str) -> dict[str, Any]:
    """Return the deterministic synthetic attestation for one capability."""
    payload: dict[str, Any] = {
        "attestation": SYNTHETIC_FIXTURE_ATTESTATION,
        "capability": capability,
        "note": (
            "Synthetic fixture bytes only; this file is not approval evidence "
            "and authorizes nothing."
        ),
    }
    payload.update(_ATTESTATION_FIELDS.get(capability, {}))
    return payload


def _dumps(payload: dict[str, Any]) -> bytes:
    """Serialize one bundle file deterministically."""
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def bundle_files() -> dict[str, bytes]:
    """Return the complete bundle as a path-to-bytes mapping."""
    files: dict[str, bytes] = {}
    for capability in REQUIRED_CAPABILITIES:
        evidence_relative = f"{EVIDENCE_DIRECTORY}/{capability}.json"
        evidence_bytes = _dumps(_evidence_payload(capability))
        files[evidence_relative] = evidence_bytes
        record = {
            "capability": capability,
            "owner": SYNTHETIC_OWNER,
            "approval_reference": SYNTHETIC_APPROVAL,
            "scope": (
                f"Synthetic fixture for the {capability} capability; covers "
                "no real system, provider or data."
            ),
            "system": SYSTEM_IDENTIFIER,
            "environment": SYNTHETIC_ENVIRONMENT,
            "issued_at": SYNTHETIC_ISSUED_AT,
            "review_by": SYNTHETIC_REVIEW_BY,
            "evidence_path": evidence_relative,
            "evidence_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
            "synthetic": True,
        }
        files[f"{RECORDS_DIRECTORY}/{capability}{RECORD_SUFFIX}"] = _dumps(record)
    return files


def write_bundle(root: Path) -> None:
    """Write the deterministic bundle under ``root``."""
    for relative, content in bundle_files().items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def bundle_matches(root: Path) -> bool:
    """Return True when ``root`` holds exactly the generated bundle."""
    expected = bundle_files()
    for relative, content in expected.items():
        path = root / relative
        if not path.is_file() or path.read_bytes() != content:
            return False
    actual = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}
    return actual == set(expected)


def main() -> int:
    """Regenerate the checked-in synthetic bundle."""
    root = default_synthetic_root()
    write_bundle(root)
    sys.stdout.write(f"wrote synthetic evidence bundle to {root}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
