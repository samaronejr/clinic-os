"""Authenticate owner records and their candidate, provider and source bindings."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import psycopg
import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ops.release import profiles

if TYPE_CHECKING:
    from collections.abc import Mapping

RECORD_FIELDS: Final = frozenset(
    {
        "reference",
        "profile",
        "release_id",
        "commit_sha",
        "tree_sha",
        "owner",
        "issued_at",
        "review_by",
        "evidence_path",
        "evidence_sha256",
        "owner_signature",
    }
)
REVERIFICATION_LOG: Final = (
    Path(__file__).resolve().parents[2] / "docs/compliance/reverification-log.md"
)
MAX_REVERIFICATION_AGE: Final = timedelta(days=90)
REVERIFICATION_COLUMNS: Final = 8


@dataclass(frozen=True, slots=True)
class Evaluation:
    """One immutable evaluation context, including the external trust anchor."""

    root: Path
    profile: profiles.ReleaseProfile
    release_id: str
    now: datetime
    environment: Mapping[str, str]


def _hex(value: object, length: int) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None
    )


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _signature_findings(record: dict[str, Any], context: Evaluation) -> list[str]:
    signature = record["owner_signature"]
    if (
        not isinstance(signature, str)
        or re.fullmatch(r"ed25519:[0-9a-f]{128}", signature) is None
    ):
        return ["owner_signature must use ed25519:<128 lowercase hex characters>"]
    public_key = context.environment.get(profiles.OWNER_PUBLIC_KEY_ENV, "")
    if not _hex(public_key, 64):
        return ["trusted owner public key is missing or malformed"]
    unsigned = {key: value for key, value in record.items() if key != "owner_signature"}
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key)).verify(
            bytes.fromhex(signature.removeprefix("ed25519:")),
            rfc8785.dumps(unsigned),
        )
    except (InvalidSignature, ValueError, rfc8785.CanonicalizationError):
        return ["owner signature does not authenticate this record"]
    return []


def _binding_findings(
    record: dict[str, Any], reference: str, context: Evaluation
) -> list[str]:
    expected = {
        "reference": reference,
        "profile": context.profile.name,
        "release_id": context.release_id,
        "commit_sha": context.environment.get(profiles.COMMIT_ENV),
        "tree_sha": context.environment.get(profiles.TREE_ENV),
    }
    findings = [
        f"{key} does not bind the release candidate"
        for key, value in expected.items()
        if not value or record[key] != value
    ]
    findings.extend(
        f"{key} must be a full Git object id"
        for key in ("commit_sha", "tree_sha")
        if not _hex(record[key], 40)
    )
    if not isinstance(record["owner"], str) or not record["owner"].strip():
        findings.append("owner must name the accountable owner")
    issued = _timestamp(record["issued_at"])
    review = _timestamp(record["review_by"])
    if issued is None or issued > context.now:
        findings.append("issued_at must be timezone-aware and not future")
    if review is None or review <= context.now:
        findings.append("review_by must be timezone-aware and future")
    return findings


def _payload(
    record: dict[str, Any], context: Evaluation
) -> tuple[dict[str, Any] | None, list[str]]:
    relative = record["evidence_path"]
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
    ):
        return None, ["evidence_path must stay inside the evidence root"]
    path = context.root / relative
    if not path.resolve().is_relative_to(context.root.resolve()):
        return None, ["evidence_path escapes the evidence root"]
    try:
        content = path.read_bytes()
        payload = json.loads(content)
    except (OSError, ValueError):
        return None, ["evidence must be readable JSON"]
    if (
        not _hex(record["evidence_sha256"], 64)
        or hashlib.sha256(content).hexdigest() != record["evidence_sha256"]
    ):
        return None, ["evidence digest does not match"]
    failure = None
    if not isinstance(payload, dict):
        failure = "evidence must be a JSON object"
    elif payload.get("synthetic") is True or payload.get("data_class") == "synthetic":
        failure = "synthetic evidence cannot satisfy a live profile"
    return (None, [failure]) if failure else (payload, [])


def provider_findings(
    key: str, payload: dict[str, Any], environment: Mapping[str, str]
) -> list[str]:
    """Read the actual registry without importing Django during settings startup."""
    dsn = environment.get("APP_DATABASE_URL", "")
    if not dsn:
        return ["provider registry connection is required"]
    try:
        with psycopg.connect(dsn, connect_timeout=3) as connection:
            row = connection.execute(
                "SELECT v.id::text, a.id::text "
                "FROM clinic_app.providers_providercapability c "
                "JOIN clinic_app.providers_capabilityversion v "
                "ON v.id = c.current_version_id AND v.capability_id = c.id "
                "JOIN clinic_app.providers_capabilityapproval initial_approval "
                "ON initial_approval.id = v.approval_id "
                "AND initial_approval.capability_id = c.id "
                "JOIN clinic_app.providers_activationrecord a "
                "ON a.version_id = v.id AND a.capability_id = c.id "
                "JOIN clinic_app.providers_capabilityapproval activation_approval "
                "ON activation_approval.id = a.approval_id "
                "AND activation_approval.capability_id = c.id "
                "WHERE c.key = %s AND c.clinic_id IS NULL "
                "AND v.state = 'activated' AND v.environment = 'live' "
                "ORDER BY a.activated_at DESC LIMIT 1",
                (key,),
            ).fetchone()
    except psycopg.Error:
        return ["provider registry is unavailable"]
    if row is None or row != (
        payload.get("version_id"),
        payload.get("activation_id"),
    ):
        return ["provider requires its current activated version and activation record"]
    return []


def owner_findings(environment: Mapping[str, str]) -> list[str]:
    """Activation is an owner operation; a runtime connection cannot perform it."""
    dsn = environment.get("MIGRATION_DATABASE_URL", "")
    if not dsn:
        return ["owner database connection is required for profile activation"]
    try:
        with psycopg.connect(dsn, connect_timeout=3) as connection:
            row = connection.execute("SELECT current_user").fetchone()
    except psycopg.Error:
        return ["owner database connection is unavailable"]
    return (
        [] if row == ("clinic_owner",) else ["profile activation requires clinic_owner"]
    )


def _reverification_findings(
    reference: str, payload: dict[str, Any], context: Evaluation
) -> list[str]:
    step = reference.removeprefix("reverification-")
    checked = _timestamp(payload.get("checked_at"))
    if (
        checked is None
        or checked > context.now
        or context.now - checked > MAX_REVERIFICATION_AGE
        or payload.get("step") != step
        or payload.get("outcome") != "verified"
    ):
        return ["dependent source row must be verified within 90 days"]
    try:
        rows = [
            row.split("|")
            for row in REVERIFICATION_LOG.read_text(encoding="utf-8").splitlines()
            if row.startswith(f"| {step} |")
        ]
    except OSError:
        return ["reverification log is unavailable"]
    if len(rows) != 1 or len(rows[0]) != REVERIFICATION_COLUMNS:
        return ["dependent row is missing from the reverification log"]
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", rows[0][4])
    if (
        not rows[0][5].strip().startswith("Closed:")
        or not dates
        or checked.date() != max(date.fromisoformat(value) for value in dates)
    ):
        return ["reverification record does not match a closed dated source row"]
    return []


def _external_findings(
    reference: str, payload: dict[str, Any], context: Evaluation
) -> list[str]:
    if reference.startswith("EG-"):
        return (
            []
            if payload.get("gate") == reference
            and payload.get("decision") == "approved"
            else ["external gate requires its accountable approval record"]
        )
    if reference.startswith("evaluation-"):
        return _evaluation_findings(reference, payload)
    if reference == "disaster-recovery":
        if (
            payload.get("verdict") != "passed"
            or payload.get("provider_pitr_verified") is not True
            or payload.get("encrypted_provider_backup_verified") is not True
        ):
            return ["provider PITR and encrypted provider recovery evidence required"]
        return []
    return _candidate_findings(reference, payload, context)


def _evaluation_findings(reference: str, payload: dict[str, Any]) -> list[str]:
    try:
        UUID(str(payload.get("run_id")))
    except ValueError:
        return ["evaluation requires its immutable run identifier"]
    if (
        payload.get("capability") != reference.removeprefix("evaluation-")
        or payload.get("evaluation_status") != "passed"
        or payload.get("real_world_performance") is not True
        or payload.get("human_review_complete") is not True
        or not _hex(payload.get("corpus_sha256"), 64)
        or not _hex(payload.get("thresholds_sha256"), 64)
    ):
        return ["evaluation requires a passed human-reviewed non-synthetic run"]
    return []


def _candidate_findings(
    reference: str, payload: dict[str, Any], context: Evaluation
) -> list[str]:
    expected = {
        "commit_sha": context.environment.get(profiles.COMMIT_ENV),
        "tree_sha": context.environment.get(profiles.TREE_ENV),
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        return ["evidence does not bind the candidate commit and tree"]
    if reference == "security-assessment":
        return (
            []
            if payload.get("verdict") == "accepted"
            else ["independent security assessment must be accepted by the owner"]
        )
    run_id = payload.get("hosted_run_id")
    if (
        not isinstance(run_id, str)
        or re.fullmatch(r"[1-9][0-9]*", run_id) is None
        or payload.get("conclusion") != "success"
    ):
        return ["candidate requires its own successful hosted run id"]
    return []


def _content_findings(
    reference: str, payload: dict[str, Any], context: Evaluation
) -> list[str]:
    if reference.startswith("provider-"):
        return provider_findings(
            reference.removeprefix("provider-"), payload, context.environment
        )
    if reference.startswith("reverification-"):
        return _reverification_findings(reference, payload, context)
    return _external_findings(reference, payload, context)


def evaluate(context: Evaluation) -> dict[str, Any]:
    """Return exact missing-record lists; a signed assertion is never activation."""
    directory = context.root / "profiles" / context.profile.name
    records: dict[str, Any] = {}
    for reference in context.profile.required_records:
        path = directory / f"{reference}.record.json"
        findings: list[str] = []
        try:
            record = json.loads(path.read_bytes())
        except (OSError, ValueError):
            findings = ["no readable owner approval record supplied"]
        else:
            if not path.resolve().is_relative_to(context.root.resolve()):
                findings = ["approval record escapes the evidence root"]
            elif not isinstance(record, dict) or set(record) != RECORD_FIELDS:
                findings = ["owner approval record violates the closed schema"]
            else:
                findings = [
                    *_signature_findings(record, context),
                    *_binding_findings(record, reference, context),
                ]
                if not findings:
                    payload, findings = _payload(record, context)
                    if payload is not None:
                        findings.extend(_content_findings(reference, payload, context))
        records[reference] = {"satisfied": not findings, "findings": findings}
    expected = {f"{reference}.record.json" for reference in records}
    errors = [
        "profile contains an unexpected approval record"
        for path in sorted(directory.glob("*.record.json"))
        if path.name not in expected
    ]
    return {
        "records": records,
        "missing_records": [
            reference
            for reference, status in records.items()
            if not status["satisfied"]
        ],
        "errors": errors,
    }
