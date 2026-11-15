"""Test-only signed records; no fixture grants external approval."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from ops.release import profile_records, profiles

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pytest

COMMIT = "a" * 40
TREE = "b" * 40
RELEASE = "synthetic-profile-contract"
ISSUED = "2026-09-24T00:00:00+00:00"
REVIEW = "2099-01-01T00:00:00+00:00"


def record_path(root: Path, profile: str, reference: str) -> Path:
    return root / "profiles" / profile / f"{reference}.record.json"


def write_record(
    root: Path,
    reference: str,
    payload: dict[str, Any],
    context: tuple[Mapping[str, str], Ed25519PrivateKey],
) -> dict[str, Any]:
    environment, key = context
    profile = environment[profiles.PROFILE_ENV]
    evidence = root / "profile-evidence" / profile / f"{reference}.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, sort_keys=True).encode()
    evidence.write_bytes(content)
    record = {
        "reference": reference,
        "profile": profile,
        "release_id": environment["CLINIC_RELEASE_ID"],
        "commit_sha": environment[profiles.COMMIT_ENV],
        "tree_sha": environment[profiles.TREE_ENV],
        "owner": "Synthetic Contract Owner",
        "issued_at": ISSUED,
        "review_by": REVIEW,
        "evidence_path": str(evidence.relative_to(root)),
        "evidence_sha256": hashlib.sha256(content).hexdigest(),
    }
    record["owner_signature"] = "ed25519:" + key.sign(rfc8785.dumps(record)).hex()
    path = record_path(root, profile, reference)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return record


def signed_bundle(
    root: Path, *, profile: str = "core-live", release_id: str = RELEASE
) -> tuple[dict[str, str], Ed25519PrivateKey]:
    key = Ed25519PrivateKey.generate()
    environment = {
        profiles.PROFILE_ENV: profile,
        profiles.COMMIT_ENV: COMMIT,
        profiles.TREE_ENV: TREE,
        profiles.OWNER_PUBLIC_KEY_ENV: key.public_key().public_bytes_raw().hex(),
        "CLINIC_RELEASE_ID": release_id,
    }
    for reference in profiles.PROFILES[profile].required_records:
        payload: dict[str, Any] = {
            "gate": reference,
            "decision": "approved",
            "commit_sha": COMMIT,
            "tree_sha": TREE,
            "hosted_run_id": "99999999999",
            "conclusion": "success",
            "verdict": ("accepted" if reference == "security-assessment" else "passed"),
            "provider_pitr_verified": True,
            "encrypted_provider_backup_verified": True,
            "version_id": "test-only-version",
            "activation_id": "test-only-activation",
            "capability": reference.removeprefix("evaluation-"),
            "evaluation_status": "passed",
            "real_world_performance": True,
            "human_review_complete": True,
            "run_id": "00000000-0000-4000-8000-000000000077",
            "corpus_sha256": "c" * 64,
            "thresholds_sha256": "d" * 64,
            "step": reference.removeprefix("reverification-"),
            "checked_at": "2026-09-24T00:00:00+00:00",
            "outcome": "verified",
        }
        if reference == "reverification-RV-06":
            payload["checked_at"] = "2026-11-11T00:00:00+00:00"
        write_record(root, reference, payload, (environment, key))
    return environment, key


def simulate_profile_approval(
    monkeypatch: pytest.MonkeyPatch, environment: dict[str, str]
) -> dict[str, str]:
    """Extend the old activation simulation with authenticated test-only approvals.

    The old tests simulate a not-yet-approved managed secret backend and use
    TEST-NET storage endpoints. Only their provider-registry connection is
    simulated here; signature, expiry, source currency, profile and manifest
    checks remain real. Real registry/owner-role checks live in test_profiles.
    """
    root = Path(environment["CLINIC_RELEASE_EVIDENCE_ROOT"])
    fixture, _key = signed_bundle(root, release_id=environment["CLINIC_RELEASE_ID"])
    monkeypatch.setattr(profile_records, "provider_findings", lambda *_args: [])
    environment.update(fixture)
    environment["MIGRATION_DATABASE_URL"] = os.environ["MIGRATION_DATABASE_URL"]
    return environment


def context(
    root: Path, environment: Mapping[str, str], *, now: datetime | None = None
) -> profile_records.Evaluation:
    return profile_records.Evaluation(
        root,
        profiles.PROFILES[environment[profiles.PROFILE_ENV]],
        environment["CLINIC_RELEASE_ID"],
        now or datetime.now(UTC),
        environment,
    )
