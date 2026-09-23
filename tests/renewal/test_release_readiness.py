"""Task-44 release-readiness evidence validation tests.

Fixtures are marked synthetic or built as test-only approval-shaped records;
nothing here is copied into the real compliance documents, and no LIVE-DATA-GATE
box is checked on an agent's authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from ops.release import readiness
from ops.release.synthetic_evidence import bundle_matches

REPOSITORY = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)
ISSUED_AT = "2026-09-12T00:00:00+00:00"
REVIEW_BY = "2027-03-01T00:00:00+00:00"
RELEASE_ID = "test-release-2026-09-23"

_run_process = subprocess.run

# Capabilities the plan names as independently gating live readiness; tests
# remove each one on its own.
GATING_CAPABILITIES = (
    "dpo_designation",
    "dpo_public_contact",
    "incident_record_retention",
    "data_at_rest",
    "tenant_key_management",
    "managed_secrets",
    "tls_transport",
)

# Attestation fields the attested capabilities must carry in their evidence
# files; defined here independently of the shipped bundle builder.
LIVE_ATTESTATIONS: dict[str, dict[str, Any]] = {
    "dpo_designation": {
        "formal_designation": True,
        "named_accountable_person": "Test DPO Name",
    },
    "dpo_public_contact": {
        "published": True,
        "validated": True,
        "public_contact": "dpo@example.com",
    },
    "incident_record_retention": {
        "minimum_years": 5,
        "measured_from": "registration",
        "covers_unnotified": True,
    },
}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _record_path(root: Path, capability: str) -> Path:
    return root / "records" / f"{capability}.record.json"


def _evidence_path(root: Path, capability: str) -> Path:
    return root / "evidence" / f"{capability}.json"


def _load_record(root: Path, capability: str) -> dict[str, Any]:
    record: dict[str, Any] = json.loads(_record_path(root, capability).read_text())
    return record


def _store_record(root: Path, capability: str, record: dict[str, Any]) -> None:
    _write_json(_record_path(root, capability), record)


def _live_bundle(root: Path) -> Path:
    """Write a complete, valid live-mode record set built for these tests."""
    for capability in readiness.REQUIRED_CAPABILITIES:
        evidence = {
            "attestation": "test-only approval evidence",
            "capability": capability,
            **LIVE_ATTESTATIONS.get(capability, {}),
        }
        evidence_bytes = (
            json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        ).encode()
        evidence_file = _evidence_path(root, capability)
        evidence_file.parent.mkdir(parents=True, exist_ok=True)
        evidence_file.write_bytes(evidence_bytes)
        record = {
            "capability": capability,
            "owner": "Test Accountable Owner",
            "approval_reference": "test-approval-001",
            "scope": f"Test scope covering {capability} for the release.",
            "system": readiness.SYSTEM_IDENTIFIER,
            "environment": "live",
            "issued_at": ISSUED_AT,
            "review_by": REVIEW_BY,
            "evidence_path": f"evidence/{capability}.json",
            "evidence_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
            "synthetic": False,
        }
        _store_record(root, capability, record)
    return root


def _evaluate(root: Path, mode: str = "live") -> dict[str, Any]:
    return readiness.evaluate_evidence(root, mode, RELEASE_ID, NOW)


def _findings(report: dict[str, Any], capability: str) -> list[str]:
    findings: list[str] = report["capabilities"][capability]["findings"]
    return findings


def _cli(
    *arguments: str, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop(readiness.EVIDENCE_ROOT_ENV, None)
    env.pop(readiness.RELEASE_ID_ENV, None)
    if environment:
        env.update(environment)
    return _run_process(
        [sys.executable, "-m", "ops.release.readiness", *arguments],
        cwd=REPOSITORY,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_shipped_synthetic_bundle_matches_generator_and_passes() -> None:
    root = readiness.default_synthetic_root()
    assert bundle_matches(root)
    report = _evaluate(root, "synthetic")
    assert report["ready"] is True
    assert report["missing"] == []
    assert report["errors"] == []
    live = report["live_gap_report"]
    assert live["ready"] is False
    assert sorted(live["missing"]) == sorted(readiness.REQUIRED_CAPABILITIES)
    assert all(
        any(
            "never accepted for live" in finding
            for finding in live["capabilities"][capability]["findings"]
        )
        for capability in readiness.REQUIRED_CAPABILITIES
    )


def test_complete_live_bundle_is_accepted(tmp_path: Path) -> None:
    report = _evaluate(_live_bundle(tmp_path))
    assert report["ready"] is True
    assert report["missing"] == []
    assert report["errors"] == []
    assert report["release_id"] == RELEASE_ID


def test_report_covers_every_required_capability(tmp_path: Path) -> None:
    report = _evaluate(tmp_path)
    assert report["ready"] is False
    assert set(report["capabilities"]) == set(readiness.REQUIRED_CAPABILITIES)
    assert len(report["capabilities"]) == 28
    assert sorted(report["missing"]) == sorted(readiness.REQUIRED_CAPABILITIES)
    assert report["capabilities"]["dpo_designation"]["findings"] == [
        "no approval record supplied"
    ]
    assert "not legal review" in report["disclaimer"]
    assert "not legal review" in readiness.DISCLAIMER


@pytest.mark.parametrize("capability", GATING_CAPABILITIES)
def test_each_gating_capability_blocks_live_independently(
    tmp_path: Path, capability: str
) -> None:
    root = _live_bundle(tmp_path)
    _record_path(root, capability).unlink()
    report = _evaluate(root)
    assert report["ready"] is False
    assert report["missing"] == [capability]
    assert _findings(report, capability) == ["no approval record supplied"]


def test_synthetic_record_never_satisfies_live(tmp_path: Path) -> None:
    root = _live_bundle(tmp_path)
    record = _load_record(root, "data_at_rest")
    record["synthetic"] = True
    _store_record(root, "data_at_rest", record)
    report = _evaluate(root)
    assert report["ready"] is False
    assert report["missing"] == ["data_at_rest"]
    assert any(
        "never accepted for live" in finding
        for finding in _findings(report, "data_at_rest")
    )


@pytest.mark.parametrize("capability", readiness.REQUIRED_CAPABILITIES)
def test_shipped_fixture_bytes_never_satisfy_live(
    tmp_path: Path, capability: str
) -> None:
    """Substituting shipped fixture bytes must fail even with a valid digest."""
    root = _live_bundle(tmp_path)
    fixture = (
        readiness.default_synthetic_root() / "evidence" / f"{capability}.json"
    ).read_bytes()
    _evidence_path(root, capability).write_bytes(fixture)
    record = _load_record(root, capability)
    record["evidence_sha256"] = hashlib.sha256(fixture).hexdigest()
    assert record["synthetic"] is False
    _store_record(root, capability, record)
    report = _evaluate(root)
    assert report["ready"] is False
    assert report["missing"] == [capability]
    assert any(
        "synthetic fixture" in finding for finding in _findings(report, capability)
    )


def test_fixture_marked_evidence_never_satisfies_live(tmp_path: Path) -> None:
    """Bytes still attesting themselves a fixture fail even when modified."""
    capability = "managed_secrets"
    root = _live_bundle(tmp_path)
    fixture = json.loads(
        (
            readiness.default_synthetic_root() / "evidence" / f"{capability}.json"
        ).read_text()
    )
    assert fixture["attestation"] == readiness.SYNTHETIC_FIXTURE_ATTESTATION
    fixture["extra"] = "modified but still marked synthetic"
    _write_json(_evidence_path(root, capability), fixture)
    record = _load_record(root, capability)
    record["evidence_sha256"] = hashlib.sha256(
        _evidence_path(root, capability).read_bytes()
    ).hexdigest()
    _store_record(root, capability, record)
    report = _evaluate(root)
    assert report["ready"] is False
    assert report["missing"] == [capability]
    assert any(
        "synthetic fixture" in finding for finding in _findings(report, capability)
    )


def test_cli_rejects_fixture_substitution(tmp_path: Path) -> None:
    """End-to-end: fixture bytes with an updated digest exit 1, not ready."""
    capability = "managed_secrets"
    root = _live_bundle(tmp_path / "live")
    fixture = (
        readiness.default_synthetic_root() / "evidence" / f"{capability}.json"
    ).read_bytes()
    _evidence_path(root, capability).write_bytes(fixture)
    record = _load_record(root, capability)
    record["evidence_sha256"] = hashlib.sha256(fixture).hexdigest()
    _store_record(root, capability, record)
    result = _cli(
        "check",
        "--mode",
        "live",
        environment={
            readiness.EVIDENCE_ROOT_ENV: str(root),
            readiness.RELEASE_ID_ENV: RELEASE_ID,
        },
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["ready"] is False
    assert report["missing"] == [capability]
    assert any(
        "synthetic fixture" in finding
        for finding in report["capabilities"][capability]["findings"]
    )


def test_incident_retention_requires_five_years_from_registration(
    tmp_path: Path,
) -> None:
    capability = "incident_record_retention"
    for index, (mutation, expected) in enumerate(
        (
            ({"minimum_years": 4}, "at least 5"),
            ({"minimum_years": "5"}, "at least 5"),
            ({"measured_from": "notification"}, "registration"),
            ({"measured_from": "closure"}, "registration"),
            ({"covers_unnotified": False}, "notified or not"),
        )
    ):
        root = _live_bundle(tmp_path / f"case-{index}")
        evidence = json.loads(_evidence_path(root, capability).read_text())
        evidence.update(mutation)
        _write_json(_evidence_path(root, capability), evidence)
        record = _load_record(root, capability)
        record["evidence_sha256"] = hashlib.sha256(
            _evidence_path(root, capability).read_bytes()
        ).hexdigest()
        _store_record(root, capability, record)
        report = _evaluate(root)
        assert report["ready"] is False
        assert report["missing"] == [capability]
        assert any(expected in finding for finding in _findings(report, capability))


def test_dpo_designation_requires_named_accountable_person(
    tmp_path: Path,
) -> None:
    capability = "dpo_designation"
    for index, (mutation, expected) in enumerate(
        (
            ({"formal_designation": False}, "formal designation"),
            ({"named_accountable_person": ""}, "accountable person"),
            ({"named_accountable_person": "   "}, "accountable person"),
        )
    ):
        root = _live_bundle(tmp_path / f"case-{index}")
        evidence = json.loads(_evidence_path(root, capability).read_text())
        evidence.update(mutation)
        _write_json(_evidence_path(root, capability), evidence)
        record = _load_record(root, capability)
        record["evidence_sha256"] = hashlib.sha256(
            _evidence_path(root, capability).read_bytes()
        ).hexdigest()
        _store_record(root, capability, record)
        report = _evaluate(root)
        assert report["ready"] is False
        assert report["missing"] == [capability]
        assert any(expected in finding for finding in _findings(report, capability))


def test_dpo_public_contact_requires_published_validated_contact(
    tmp_path: Path,
) -> None:
    capability = "dpo_public_contact"
    for index, (mutation, expected) in enumerate(
        (
            ({"published": False}, "published"),
            ({"validated": False}, "validated"),
            ({"public_contact": ""}, "public contact"),
        )
    ):
        root = _live_bundle(tmp_path / f"case-{index}")
        evidence = json.loads(_evidence_path(root, capability).read_text())
        evidence.update(mutation)
        _write_json(_evidence_path(root, capability), evidence)
        record = _load_record(root, capability)
        record["evidence_sha256"] = hashlib.sha256(
            _evidence_path(root, capability).read_bytes()
        ).hexdigest()
        _store_record(root, capability, record)
        report = _evaluate(root)
        assert report["ready"] is False
        assert report["missing"] == [capability]
        assert any(expected in finding for finding in _findings(report, capability))


def test_expired_future_and_naive_dates_rejected(tmp_path: Path) -> None:
    capability = "privacy_approval"
    for index, (field, value, expected) in enumerate(
        (
            ("review_by", "2026-01-01T00:00:00+00:00", "expired"),
            ("issued_at", "2099-01-01T00:00:00+00:00", "in the future"),
            ("review_by", "2027-03-01 00:00:00", "timezone-aware"),
            ("issued_at", "not-a-date", "timezone-aware"),
        )
    ):
        root = _live_bundle(tmp_path / f"case-{index}")
        record = _load_record(root, capability)
        record[field] = value
        _store_record(root, capability, record)
        report = _evaluate(root)
        assert report["ready"] is False
        assert report["missing"] == [capability]
        assert any(expected in finding for finding in _findings(report, capability))


def test_system_environment_and_digest_binding(tmp_path: Path) -> None:
    capability = "tls_transport"
    root = _live_bundle(tmp_path / "system")
    record = _load_record(root, capability)
    record["system"] = "other-system"
    _store_record(root, capability, record)
    report = _evaluate(root)
    assert report["missing"] == [capability]
    assert any("does not match" in finding for finding in _findings(report, capability))

    root = _live_bundle(tmp_path / "environment")
    record = _load_record(root, capability)
    record["environment"] = "staging"
    _store_record(root, capability, record)
    report = _evaluate(root)
    assert report["missing"] == [capability]
    assert any("environment" in finding for finding in _findings(report, capability))

    root = _live_bundle(tmp_path / "digest")
    _evidence_path(root, capability).write_bytes(b"tampered evidence bytes")
    report = _evaluate(root)
    assert report["missing"] == [capability]
    assert any(
        "digest mismatch" in finding for finding in _findings(report, capability)
    )

    root = _live_bundle(tmp_path / "digest-format")
    record = _load_record(root, capability)
    record["evidence_sha256"] = "Z" * 64
    _store_record(root, capability, record)
    report = _evaluate(root)
    assert report["missing"] == [capability]
    assert any("SHA-256" in finding for finding in _findings(report, capability))


def test_evidence_path_stays_inside_root(tmp_path: Path) -> None:
    capability = "managed_secrets"
    for index, bad_path in enumerate(
        ("../outside.json", "/etc/passwd", "evidence/../x.json")
    ):
        root = _live_bundle(tmp_path / f"case-{index}")
        record = _load_record(root, capability)
        record["evidence_path"] = bad_path
        _store_record(root, capability, record)
        report = _evaluate(root)
        assert report["ready"] is False
        assert report["missing"] == [capability]
        assert any(
            "inside the evidence root" in finding
            for finding in _findings(report, capability)
        )

    root = _live_bundle(tmp_path / "missing-file")
    record = _load_record(root, capability)
    record["evidence_path"] = "evidence/absent.json"
    _store_record(root, capability, record)
    report = _evaluate(root)
    assert report["missing"] == [capability]
    assert any("is missing" in finding for finding in _findings(report, capability))


def test_closed_schema_rejects_extra_missing_and_wrong_types(
    tmp_path: Path,
) -> None:
    capability = "credential_rotation"
    root = _live_bundle(tmp_path / "extra")
    record = _load_record(root, capability)
    record["notes"] = "unsanctioned field"
    _store_record(root, capability, record)
    report = _evaluate(root)
    assert report["missing"] == [capability]
    assert any("closed schema" in finding for finding in _findings(report, capability))

    root = _live_bundle(tmp_path / "missing-owner")
    record = _load_record(root, capability)
    del record["owner"]
    _store_record(root, capability, record)
    report = _evaluate(root)
    assert report["missing"] == [capability]
    assert any("closed schema" in finding for finding in _findings(report, capability))

    root = _live_bundle(tmp_path / "blank-owner")
    record = _load_record(root, capability)
    record["owner"] = "  "
    _store_record(root, capability, record)
    report = _evaluate(root)
    assert report["missing"] == [capability]
    assert any("accountable" in finding for finding in _findings(report, capability))

    root = _live_bundle(tmp_path / "synthetic-type")
    record = _load_record(root, capability)
    record["synthetic"] = "yes"
    _store_record(root, capability, record)
    report = _evaluate(root)
    assert report["missing"] == [capability]
    assert any("boolean" in finding for finding in _findings(report, capability))


def test_unknown_duplicate_and_misfiled_records_rejected(tmp_path: Path) -> None:
    root = _live_bundle(tmp_path / "unknown")
    record = _load_record(root, "email")
    _record_path(root, "email").unlink()
    record["capability"] = "not_a_capability"
    _write_json(root / "records" / "not_a_capability.record.json", record)
    report = _evaluate(root)
    assert report["ready"] is False
    assert report["missing"] == ["email"]
    assert any("unrecognized capability" in error for error in report["errors"])

    root = _live_bundle(tmp_path / "duplicate")
    record = _load_record(root, "sms")
    _write_json(root / "records" / "sms-copy.record.json", record)
    report = _evaluate(root)
    assert report["ready"] is False
    assert any("same capability" in error for error in report["errors"])

    root = _live_bundle(tmp_path / "misfiled")
    record = _load_record(root, "whatsapp")
    _record_path(root, "whatsapp").unlink()
    _write_json(root / "records" / "renamed.record.json", record)
    report = _evaluate(root)
    assert report["ready"] is False
    assert report["missing"] == ["whatsapp"]
    assert any(
        "must be stored as" in finding for finding in _findings(report, "whatsapp")
    )


def test_unreadable_record_and_attestation_rejected(tmp_path: Path) -> None:
    root = _live_bundle(tmp_path / "bad-record")
    _record_path(root, "ropa").write_text("{not json")
    report = _evaluate(root)
    assert report["ready"] is False
    assert report["missing"] == ["ropa"]
    assert any("not readable JSON" in error for error in report["errors"])

    root = _live_bundle(tmp_path / "bad-attestation")
    capability = "incident_record_retention"
    _evidence_path(root, capability).write_bytes(b"[1, 2, 3]")
    record = _load_record(root, capability)
    record["evidence_sha256"] = hashlib.sha256(b"[1, 2, 3]").hexdigest()
    _store_record(root, capability, record)
    report = _evaluate(root)
    assert report["missing"] == [capability]
    assert any("JSON object" in finding for finding in _findings(report, capability))


def test_cli_contract(tmp_path: Path) -> None:
    synthetic = _cli("check", "--mode", "synthetic")
    assert synthetic.returncode == 0
    report = json.loads(synthetic.stdout)
    assert report["ready"] is True
    assert report["live_gap_report"]["ready"] is False

    live_without_env = _cli("check", "--mode", "live")
    assert live_without_env.returncode == 2
    assert readiness.EVIDENCE_ROOT_ENV in live_without_env.stderr

    missing_root = _cli(
        "check",
        "--mode",
        "live",
        environment={
            readiness.EVIDENCE_ROOT_ENV: str(tmp_path / "absent"),
            readiness.RELEASE_ID_ENV: RELEASE_ID,
        },
    )
    assert missing_root.returncode == 2

    live_root = _live_bundle(tmp_path / "live")
    live_ok = _cli(
        "check",
        "--mode",
        "live",
        environment={
            readiness.EVIDENCE_ROOT_ENV: str(live_root),
            readiness.RELEASE_ID_ENV: RELEASE_ID,
        },
    )
    assert live_ok.returncode == 0
    assert json.loads(live_ok.stdout)["ready"] is True

    live_without_release_id = _cli(
        "check",
        "--mode",
        "live",
        environment={readiness.EVIDENCE_ROOT_ENV: str(live_root)},
    )
    assert live_without_release_id.returncode == 2
    assert readiness.RELEASE_ID_ENV in live_without_release_id.stderr

    synthetic_root = readiness.default_synthetic_root()
    live_rejected = _cli(
        "check",
        "--mode",
        "live",
        environment={
            readiness.EVIDENCE_ROOT_ENV: str(synthetic_root),
            readiness.RELEASE_ID_ENV: RELEASE_ID,
        },
    )
    assert live_rejected.returncode == 1
    rejected_report = json.loads(live_rejected.stdout)
    assert rejected_report["ready"] is False
    assert sorted(rejected_report["missing"]) == sorted(readiness.REQUIRED_CAPABILITIES)

    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    synthetic_incomplete = _cli(
        "check",
        "--mode",
        "synthetic",
        environment={readiness.EVIDENCE_ROOT_ENV: str(empty_root)},
    )
    assert synthetic_incomplete.returncode == 1
    incomplete = json.loads(synthetic_incomplete.stdout)
    assert incomplete["ready"] is False
    assert sorted(incomplete["missing"]) == sorted(readiness.REQUIRED_CAPABILITIES)


def test_failed_validation_leaves_live_mode_disabled(tmp_path: Path) -> None:
    root = _live_bundle(tmp_path)
    _record_path(root, "dpo_designation").unlink()
    report = _evaluate(root)
    assert report["ready"] is False

    environment = os.environ.copy()
    environment.update(
        {
            "CLINIC_DATA_MODE": "live",
            "DJANGO_SETTINGS_MODULE": "config.settings.test",
        }
    )
    result = _run_process(
        [sys.executable, "-c", "import config.settings.base"],
        cwd=REPOSITORY,
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )
    assert result.returncode != 0
    assert "live data mode is not approved" in result.stderr

    gate = (REPOSITORY / "docs/compliance/LIVE-DATA-GATE.md").read_text(
        encoding="utf-8"
    )
    assert "- [ ] legal approval" in gate
    assert "- [x]" not in gate
