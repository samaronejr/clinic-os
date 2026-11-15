"""Todo 77: exact bundle refusals, authenticated records and real registry reads."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import rfc8785
from apps.providers import lifecycle, services
from apps.providers.models import ActivationRecord
from django.db import connection
from ops.release import activation, profile_records, profiles, readiness

from database_urls import database_url_for_name
from provider_gate_support import APPROVER, seed_capabilities
from release.profile_support import (
    RELEASE,
    context,
    record_path,
    signed_bundle,
    write_record,
)
from renewal.test_live_activation import _live_environment
from renewal.test_release_readiness import _cli, _live_bundle

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

CORE = (
    "EG-1",
    "EG-2",
    "EG-12",
    "release-candidate",
    "security-assessment",
    "disaster-recovery",
    "provider-hosted_pitr",
    "provider-data_at_rest",
    "provider-tenant_key_management",
    "provider-managed_secrets",
    "provider-tls_transport",
    "provider-attachment_storage",
    "provider-attachment_scanning",
    "provider-pdf_rendering",
)
RV = tuple(f"reverification-RV-{number:02d}" for number in (4, 5, 6, 7))
EXPECTED = {
    "synthetic-demo": (),
    "core-live": (*CORE, *RV),
    "intelligence-live": (
        *CORE[:3],
        "EG-3",
        "EG-5",
        "EG-11",
        *CORE[3:],
        "provider-asr",
        "provider-llm_inference",
        "provider-object_storage_media",
        "evaluation-AI-01",
        "evaluation-AI-02",
        "evaluation-AI-03",
        *RV,
        "reverification-RV-01",
    ),
    "revenue-live": (
        *CORE[:3],
        "EG-5",
        "EG-8",
        "EG-9",
        *CORE[3:],
        "provider-pix",
        "provider-psp_card",
        "provider-nfse",
        "provider-tiss",
        *RV,
        "reverification-RV-11",
        "reverification-RV-12",
        "reverification-RV-13",
    ),
    "regulated-rx-live": (
        *CORE[:3],
        "EG-6",
        *CORE[3:],
        "provider-physician_registration",
        "provider-qualified_signing",
        "provider-signature_verification",
        "provider-sncr",
        *RV,
        "reverification-RV-08",
    ),
    "network-live": (
        *CORE[:3],
        "EG-7",
        *CORE[3:],
        "provider-rnds",
        *RV,
        "reverification-RV-09",
    ),
}


@pytest.mark.parametrize("name", EXPECTED)
def test_each_profile_has_exact_missing_record_list(tmp_path: Path, name: str) -> None:
    profile = profiles.PROFILES[name]
    assert profile.required_records == EXPECTED[name]
    report = profile_records.evaluate(
        profile_records.Evaluation(tmp_path, profile, RELEASE, datetime.now(UTC), {})
    )
    assert report["missing_records"] == list(EXPECTED[name])
    assert report["errors"] == []


def test_synthetic_demo_passes_and_is_labelled() -> None:
    result = _cli("check", "--mode", "synthetic", "--profile", "synthetic-demo")
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["ready"] is True
    assert report["missing_records"] == []
    assert set(report["availability"].values()) == {"synthetic"}
    assert report["live_gap_report"]["ready"] is False


@pytest.mark.parametrize("profile", ["core-live", "synthetic-demo"])
def test_profile_cannot_change_the_data_mode(tmp_path: Path, profile: str) -> None:
    mode = "synthetic" if profile == "core-live" else "live"
    report = readiness.evaluate_release(
        _live_bundle(tmp_path),
        mode,
        RELEASE,
        profile_name=profile,
        environment={},
    )
    assert report["ready"] is False
    assert "release profile does not match" in " ".join(report["errors"])
    assert set(report["availability"].values()) == {"unavailable"}


@pytest.fixture
def approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, str], Ed25519PrivateKey]:
    root = _live_bundle(tmp_path)
    environment, key = signed_bundle(root)
    monkeypatch.setattr(profile_records, "provider_findings", lambda *_args: [])
    return root, environment, key


def test_signed_profile_accepts_only_its_features(
    approved: tuple[Path, dict[str, str], Ed25519PrivateKey],
) -> None:
    root, environment, _key = approved
    report = readiness.evaluate_release(
        root, "live", RELEASE, profile_name="core-live", environment=environment
    )
    assert report["ready"] is True
    assert report["availability"]["ehr"] == "available"
    assert report["availability"]["AI-01"] == "unavailable"
    assert report["availability"]["regulated_rx"] == "unavailable"
    assert "video" not in report["required_capabilities"]


@pytest.mark.parametrize("reference", EXPECTED["core-live"])
def test_each_independent_record_gates_core_profile(
    approved: tuple[Path, dict[str, str], Ed25519PrivateKey], reference: str
) -> None:
    root, environment, _key = approved
    record_path(root, "core-live", reference).unlink()
    assert profile_records.evaluate(context(root, environment))["missing_records"] == [
        reference
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner_signature", ""),
        ("owner_signature", "ed25519:" + "0" * 128),
        ("owner_signature", "owner-approved"),
        ("release_id", "replayed-release"),
        ("profile", "network-live"),
        ("commit_sha", "f" * 40),
        ("tree_sha", "f" * 40),
        ("issued_at", "2099-01-01T00:00:00+00:00"),
        ("review_by", "2020-01-01T00:00:00+00:00"),
        ("owner", ""),
    ],
)
def test_forgery_tampering_and_replay_refuse(
    approved: tuple[Path, dict[str, str], Ed25519PrivateKey],
    field: str,
    value: str,
) -> None:
    root, environment, _key = approved
    path = record_path(root, "core-live", "EG-1")
    record = json.loads(path.read_text())
    record[field] = value
    path.write_text(json.dumps(record))
    assert profile_records.evaluate(context(root, environment))["missing_records"] == [
        "EG-1"
    ]


@pytest.mark.parametrize(
    ("reference", "payload"),
    [
        ("EG-1", {"decision": "proposed", "gate": "EG-1"}),
        ("security-assessment", {"verdict": "scope-only"}),
        ("disaster-recovery", {"synthetic": True, "verdict": "passed"}),
        (
            "release-candidate",
            {"hosted_run_id": "36045032066", "commit_sha": "b39ae436"},
        ),
        ("reverification-RV-04", {"step": "RV-04", "outcome": "open"}),
    ],
)
def test_signed_but_inadequate_evidence_refuses(
    approved: tuple[Path, dict[str, str], Ed25519PrivateKey],
    reference: str,
    payload: dict[str, Any],
) -> None:
    root, environment, key = approved
    write_record(root, reference, payload, (environment, key))
    assert profile_records.evaluate(context(root, environment))["missing_records"] == [
        reference
    ]


def test_reverification_expires_after_ninety_days(
    approved: tuple[Path, dict[str, str], Ed25519PrivateKey],
) -> None:
    root, environment, _key = approved
    now = datetime(2026, 9, 24, tzinfo=UTC) + timedelta(days=90, seconds=1)
    report = profile_records.evaluate(context(root, environment, now=now))
    assert "reverification-RV-04" in report["missing_records"]
    assert "reverification-RV-06" not in report["missing_records"]


def test_unsigned_file_is_not_an_approval(tmp_path: Path) -> None:
    path = record_path(tmp_path, "core-live", "EG-1")
    path.parent.mkdir(parents=True)
    path.write_text('{"decision":"approved","owner":"forged"}')
    result = profile_records.evaluate(
        profile_records.Evaluation(
            tmp_path, profiles.PROFILES["core-live"], RELEASE, datetime.now(UTC), {}
        )
    )
    assert "closed schema" in result["records"]["EG-1"]["findings"][0]


@pytest.mark.parametrize("relative", ["../outside.json", "/etc/passwd"])
def test_even_owner_signed_path_escapes_refuse(
    approved: tuple[Path, dict[str, str], Ed25519PrivateKey], relative: str
) -> None:
    root, environment, key = approved
    path = record_path(root, "core-live", "EG-1")
    record = json.loads(path.read_text())
    record["evidence_path"] = relative
    record.pop("owner_signature")
    record["owner_signature"] = "ed25519:" + key.sign(rfc8785.dumps(record)).hex()
    path.write_text(json.dumps(record))
    report = profile_records.evaluate(context(root, environment))
    assert report["missing_records"] == ["EG-1"]
    assert "inside the evidence root" in report["records"]["EG-1"]["findings"][0]


def test_symlink_unknown_record_and_trust_anchor_fail_closed(
    approved: tuple[Path, dict[str, str], Ed25519PrivateKey], tmp_path: Path
) -> None:
    root, environment, _key = approved
    outside = tmp_path.parent / "outside-approval.json"
    source = record_path(root, "core-live", "EG-1")
    outside.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(outside)
    report = profile_records.evaluate(context(root, environment))
    assert report["missing_records"] == ["EG-1"]
    source.unlink()
    source.write_bytes(outside.read_bytes())
    outside.unlink()
    extra = source.with_name("EG-unknown.record.json")
    extra.write_bytes(source.read_bytes())
    assert profile_records.evaluate(context(root, environment))["errors"]
    extra.unlink()
    environment[profiles.OWNER_PUBLIC_KEY_ENV] = "0" * 64
    assert profile_records.evaluate(context(root, environment))["missing_records"] == [
        *EXPECTED["core-live"]
    ]


@pytest.mark.parametrize("capability", ["AI-01", "AI-02", "AI-03"])
def test_engineering_evaluation_never_authorizes_live_ai(
    tmp_path: Path, capability: str
) -> None:
    environment, key = signed_bundle(tmp_path, profile="intelligence-live")
    reference = f"evaluation-{capability}"
    write_record(
        tmp_path,
        reference,
        {
            "capability": capability,
            "evaluation_status": "passed",
            "human_review_complete": True,
            "real_world_performance": False,
            "data_class": "synthetic",
        },
        (environment, key),
    )
    report = profile_records.evaluate(context(tmp_path, environment))
    assert reference in report["missing_records"]
    assert "synthetic evidence" in report["records"][reference]["findings"][0]


def test_legacy_live_evidence_cannot_bypass_default_profile(tmp_path: Path) -> None:
    environment = _live_environment(tmp_path, _live_bundle(tmp_path / "evidence"))
    environment.pop(activation.LIVE_REHEARSAL_ENV, None)
    code, report = activation._activate_report(environment)
    assert code == 1
    assert report["missing_records"] == list(EXPECTED["core-live"])
    assert not Path(environment[activation.ACTIVATION_STATE_ENV]).exists()


@pytest.mark.django_db(transaction=True)
def test_provider_binding_and_owner_check_use_real_database_roles() -> None:
    seed_capabilities()
    lifecycle.propose_version(
        "attachment_storage",
        version_input=lifecycle.VersionInput(
            provider="synthetic-release-contract", environment="live"
        ),
    )
    lifecycle.approve_version("attachment_storage", decision=APPROVER)
    version = lifecycle.activate_version("attachment_storage", decision=APPROVER)
    activation_record = ActivationRecord.objects.get(version=version)
    assert activation_record.approval_id != version.approval_id
    environment = {
        key: database_url_for_name(os.environ[key], connection.settings_dict["NAME"])
        for key in ("APP_DATABASE_URL", "MIGRATION_DATABASE_URL")
    }
    payload = {
        "version_id": str(version.pk),
        "activation_id": str(activation_record.pk),
    }
    assert (
        profile_records.provider_findings("attachment_storage", payload, environment)
        == []
    )
    assert profile_records.owner_findings(environment) == []
    runtime = {
        **environment,
        "MIGRATION_DATABASE_URL": environment["APP_DATABASE_URL"],
    }
    assert profile_records.owner_findings(runtime) == [
        "profile activation requires clinic_owner"
    ]
    assert profile_records.provider_findings(
        "attachment_storage", {**payload, "activation_id": "forged"}, environment
    )
    assert profile_records.provider_findings("unknown", payload, environment)
    lifecycle.degrade_version(
        "attachment_storage", decision=APPROVER, reason="synthetic degradation"
    )
    assert profile_records.provider_findings("attachment_storage", payload, environment)


@pytest.mark.django_db(transaction=True)
def test_profile_membership_is_required_even_after_runtime_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_capabilities()
    for key in ("attachment_storage", "llm_inference"):
        lifecycle.propose_version(
            key,
            version_input=lifecycle.VersionInput(
                provider=f"synthetic-profile-{key}", environment="live"
            ),
        )
        lifecycle.approve_version(key, decision=APPROVER)
        lifecycle.activate_version(key, decision=APPROVER)
    # Isolate the bundle conjunct; the actual runtime checks have their own
    # positive/negative activation suite and are never changed in production.
    monkeypatch.setattr(services, "require_live_runtime", lambda _environment: None)
    environment = {"CLINIC_DATA_MODE": "live", profiles.PROFILE_ENV: "core-live"}
    assert services.is_live(
        "attachment_storage", clinic_id=None, environment=environment
    )
    assert not services.is_live(
        "llm_inference", clinic_id=None, environment=environment
    )
    environment[profiles.PROFILE_ENV] = "intelligence-live"
    assert services.is_live("llm_inference", clinic_id=None, environment=environment)
    environment[profiles.PROFILE_ENV] = "unknown"
    assert not services.is_live(
        "llm_inference", clinic_id=None, environment=environment
    )
