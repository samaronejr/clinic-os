from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Final

import pytest
from ops.testing.assert_visual_evidence import (
    VisualEvidenceError,
    validate_manifest,
    validate_summary,
)
from ops.testing.browser_runner_contract import selected_suites
from ops.testing.browser_suite_driver import CONFIG_KEYS
from ops.testing.browser_suites.patient import (
    PatientSuiteError,
    build_patient_suite,
)

from browser.visual_contract import (
    SERIOUS_RULES,
    VIEWPORTS,
    VisualContractError,
    blocking_violations,
    require_clean_console,
    require_no_blocking_violations,
    require_no_state_in_url,
    require_post_only_forms,
)

if TYPE_CHECKING:
    from pathlib import Path

VALID_CONFIG: Final = {
    "base_url": "http://127.0.0.1:9",
    "clinic_id": "0f9d2e7c-1111-4222-8333-444455556666",
    "password": "synthetic-secret",
    "username": "synthetic.receptionist",
}
SUMMARY: Final = {
    "blocking_violation_count": 0,
    "console_messages": [],
    "schema_version": 1,
    "suite_id": "patient",
    "total_violation_count": 0,
    "viewports": [str(item["label"]) for item in VIEWPORTS],
    "violations": [],
}


def _publish(root: Path, summary: dict[str, object]) -> Path:
    artifacts: dict[str, bytes] = {
        "browser/patient/blank-list.png": b"\x89PNG\r\n\x1a\nsynthetic",
        "browser/patient/summary.json": json.dumps(summary, sort_keys=True).encode(),
    }
    digests: dict[str, str] = {}
    for relative, content in artifacts.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        digests[relative] = hashlib.sha256(content).hexdigest()
    manifest = root / "manifest.json"
    manifest.write_bytes(
        json.dumps(
            {"artifacts": digests, "schema_version": 1, "suite_ids": ["patient"]},
            sort_keys=True,
        ).encode()
    )
    return manifest


def test_valid_publication_is_accepted(tmp_path: Path) -> None:
    manifest = _publish(tmp_path, SUMMARY)

    assert len(validate_manifest(manifest)) == 2


def test_digest_drift_is_rejected(tmp_path: Path) -> None:
    manifest = _publish(tmp_path, SUMMARY)
    (tmp_path / "browser/patient/blank-list.png").write_bytes(b"tampered")

    with pytest.raises(VisualEvidenceError, match="digest drifted"):
        validate_manifest(manifest)


def test_missing_artifact_is_rejected(tmp_path: Path) -> None:
    manifest = _publish(tmp_path, SUMMARY)
    (tmp_path / "browser/patient/blank-list.png").unlink()

    with pytest.raises(VisualEvidenceError, match="is missing"):
        validate_manifest(manifest)


def test_retained_raw_capture_is_rejected(tmp_path: Path) -> None:
    manifest = _publish(tmp_path, SUMMARY)
    (tmp_path / "session.har").write_bytes(b"{}")

    with pytest.raises(VisualEvidenceError, match="raw capture"):
        validate_manifest(manifest)


def test_blocking_violations_are_rejected(tmp_path: Path) -> None:
    summary = dict(SUMMARY)
    summary["blocking_violation_count"] = 1
    manifest = _publish(tmp_path, summary)

    with pytest.raises(VisualEvidenceError, match="blocking accessibility"):
        validate_manifest(manifest)


def test_console_output_is_rejected(tmp_path: Path) -> None:
    summary = dict(SUMMARY)
    summary["console_messages"] = ["error:boom"]
    manifest = _publish(tmp_path, summary)

    with pytest.raises(VisualEvidenceError, match="console output"):
        validate_manifest(manifest)


def test_incomplete_viewport_matrix_is_rejected() -> None:
    summary = dict(SUMMARY)
    summary["viewports"] = ["desktop-1280"]

    with pytest.raises(VisualEvidenceError, match="viewport matrix"):
        validate_summary(summary)


def test_blocking_filter_only_keeps_serious_and_critical_known_rules() -> None:
    findings = [
        {"impact": "critical", "rule": "form-label", "target": "input"},
        {"impact": "serious", "rule": "color-contrast", "target": "p"},
        {"impact": "minor", "rule": "color-contrast", "target": "p"},
        {"impact": "critical", "rule": "unknown-rule", "target": "p"},
    ]

    assert len(blocking_violations(findings)) == 2
    assert "color-contrast" in SERIOUS_RULES


def test_contract_helpers_reject_every_forbidden_shape() -> None:
    with pytest.raises(VisualContractError):
        require_no_blocking_violations(
            "screen",
            [{"impact": "critical", "rule": "form-label", "target": "input"}],
        )
    with pytest.raises(VisualContractError):
        require_clean_console("screen", ["error:boom"])
    with pytest.raises(VisualContractError):
        require_no_state_in_url("screen", "/patients/?q=Marina", ("Marina",))
    with pytest.raises(VisualContractError):
        require_no_state_in_url("screen", "/patients/Marina", ("Marina",))
    with pytest.raises(VisualContractError):
        require_post_only_forms("screen", [], [])
    with pytest.raises(VisualContractError):
        require_post_only_forms("screen", ["get"], ["/patients/"])
    with pytest.raises(VisualContractError):
        require_post_only_forms("screen", ["post"], ["/patients/?page=2"])
    require_post_only_forms("screen", ["post"], ["/patients/"])


def test_patient_suite_rejects_incomplete_configuration() -> None:
    for missing in sorted(CONFIG_KEYS):
        broken = dict(VALID_CONFIG)
        broken[missing] = ""
        with pytest.raises(PatientSuiteError):
            build_patient_suite(broken)
    with pytest.raises(PatientSuiteError):
        build_patient_suite({"base_url": VALID_CONFIG["base_url"]})
    assert callable(build_patient_suite(dict(VALID_CONFIG)))


def test_patient_is_an_available_runner_suite() -> None:
    assert selected_suites(["patient"], ["patient"]) == ("patient",)
    with pytest.raises(RuntimeError):
        selected_suites([], ["patient"])
