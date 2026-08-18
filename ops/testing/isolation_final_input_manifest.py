"""Assemble final inputs from authenticated evidence and publisher authority."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Never

from ops.testing.isolation_claim_records import claim_objects, validate_claim_id
from ops.testing.isolation_common import (
    MODE_DIRECTORY,
    IsolationError,
    JsonObject,
    directory_identity,
    raw_sha256,
)
from ops.testing.isolation_final_input_auth import (
    CandidateEvidence,
    CandidateInput,
    FinalInputFreeze,
    authenticate_candidate,
    authenticate_probe,
    authenticate_receipts,
    immutable_json,
    immutable_raw,
    prepublication_ledger_sha,
    validate_bound_file,
    validate_freeze_ledger,
)
from ops.testing.isolation_final_suites import REQUIRED_SUITES
from ops.testing.isolation_lineage import load_complete_lineage
from ops.testing.isolation_tool_identity import (
    authenticate_launcher,
    authenticate_static_amd64_launcher,
)

if TYPE_CHECKING:
    from ops.testing.isolation_ledger_store import LedgerSession


def _authenticated_manifest(
    request: FinalInputFreeze, session: LedgerSession
) -> tuple[JsonObject, JsonObject]:
    ledger = session.ledger
    validate_freeze_ledger(request, ledger)
    plan_raw = immutable_raw(request.approved_plan, "approved plan")
    sidecar_path = _approved_sidecar(ledger)
    sidecar_raw = immutable_raw(sidecar_path, "approved plan sidecar")
    if sidecar_raw != raw_sha256(plan_raw).encode() + b"\n":
        _fail("approved plan sidecar differs from the bound plan")
    _, proof_raw = immutable_json(request.proof_path, "execution-host proof")
    baseline_raw = immutable_raw(request.shared_baseline_path, "shared baseline")
    validate_bound_file(ledger.get("approved_plan"), request.approved_plan, plan_raw)
    validate_bound_file(
        ledger.get("execution_host_preflight"), request.proof_path, proof_raw
    )
    baseline = ledger.get("baseline")
    shared = (
        baseline.get("shared_evidence_manifest") if isinstance(baseline, dict) else None
    )
    validate_bound_file(shared, request.shared_baseline_path, baseline_raw)
    lineage = load_complete_lineage(request.attempt_root, request.attempt_id)
    application = _candidate(request, ledger, "application")
    runner = _candidate(request, ledger, "browser-runner")
    publisher = _publisher_gate(
        request, ledger, (application.claim_id, runner.claim_id)
    )
    receipts = authenticate_receipts(request.receipt_root)
    final_suites_raw = immutable_raw(request.final_suite_path, "final browser suites")
    expected_suites = b"availability\npatient\nruntime-https\nscheduling\n"
    if final_suites_raw != expected_suites:
        _fail("final browser suite bytes differ from the four-suite contract")
    manifest: JsonObject = {
        "application_candidate": application.manifest,
        "approved_plan_sha256": raw_sha256(plan_raw),
        "attempt_id": request.attempt_id,
        "execution_host_preflight_sha256": raw_sha256(proof_raw),
        "final_input_probe_sha256": authenticate_probe(
            request.final_probe_path, "final-input-freeze", proof_raw
        ),
        "final_required_browser_suites": list(REQUIRED_SUITES),
        "final_required_browser_suites_sha256": raw_sha256(final_suites_raw),
        "kickoff_probe_sha256": authenticate_probe(
            request.kickoff_probe_path, "todo1-kickoff", proof_raw
        ),
        "ledger_sha256": prepublication_ledger_sha(session, publisher),
        "lineage_validation_sha256": lineage.validation_sha256,
        "primary_receipts": receipts,
        "receipt_lineage_sha256": lineage.lineage_sha256,
        "runtime_bindings": _runtime_bindings(request),
        "review_inputs": {
            "approved_plan": str(request.approved_plan),
            "approved_sidecar": str(sidecar_path),
            "approved_sidecar_sha256": raw_sha256(sidecar_raw),
            "receipts": [
                {
                    "path": str(request.receipt_root / str(item["relative_path"])),
                    "sha256": item["sha256"],
                }
                for item in receipts
                if isinstance(item, dict)
            ],
        },
        "runner_candidate": runner.manifest,
        "schema_version": 1,
        "sha": request.sha,
        "shared_evidence_baseline_sha256": raw_sha256(baseline_raw),
        "terminal_publisher_claim_id": publisher["claim_id"],
        "tree_sha": request.tree_sha,
    }
    return manifest, publisher


def _runtime_bindings(request: FinalInputFreeze) -> JsonObject:
    return {
        "attempt_root": str(request.attempt_root),
        "codex_launcher": authenticate_static_amd64_launcher(request.codex_source_path),
        "failure_receipt_root": str(request.attempt_root / "final-failure-receipts"),
        "ledger_path": str(request.ledger_path),
        "review_controller_root": str(request.attempt_root / "review-lane-controllers"),
        "terminal_root": str(request.output_path.parent),
        "uv_launcher": authenticate_launcher(request.uv_source_path),
        "worktree": str(request.worktree),
    }


def _candidate(
    request: FinalInputFreeze, ledger: JsonObject, kind: str
) -> CandidateEvidence:
    runner = kind == "browser-runner"
    return authenticate_candidate(
        request,
        ledger,
        CandidateInput(
            kind,
            request.runner_envelope_path
            if runner
            else request.application_envelope_path,
            request.runner_history_path if runner else request.application_history_path,
            request.runner_inspection if runner else request.application_inspection,
        ),
    )


def _publisher_gate(
    request: FinalInputFreeze,
    ledger: JsonObject,
    candidate_ids: tuple[str, str],
) -> JsonObject:
    claims = claim_objects(ledger.get("claims"))
    if any(
        claim.get("claim_id") in candidate_ids
        or claim.get("purpose")
        in {
            "candidate-application-publisher",
            "candidate-browser-runner-publisher",
        }
        for claim in claims
    ):
        _fail("candidate publisher claim is still present")
    publishers = [
        claim
        for claim in claims
        if claim.get("purpose") == "final-terminal-publisher"
        and claim.get("status") == "active"
    ]
    if len(publishers) != 1:
        _fail("final inputs require one active persistent terminal publisher")
    publisher = publishers[0]
    try:
        claim_id = validate_claim_id(publisher.get("claim_id"))
    except IsolationError as error:
        message = "terminal publisher claim ID is invalid"
        raise IsolationError(message) from error
    claim_root = request.attempt_root / "claims" / claim_id
    if request.staging_root != claim_root:
        _fail("terminal publisher staging root binding drifted")
    if publisher.get("root_relative_path") != f"claims/{claim_id}":
        _fail("terminal publisher claim root binding drifted")
    try:
        identity = directory_identity(claim_root)
    except FileNotFoundError as error:
        message = "terminal publisher claim root is absent"
        raise IsolationError(message) from error
    if identity.get("mode") != MODE_DIRECTORY:
        _fail("terminal publisher claim root is not private")
    return publisher


def _approved_sidecar(ledger: JsonObject) -> Path:
    approved = ledger.get("approved_plan")
    if not isinstance(approved, dict):
        _fail("approved plan binding is absent")
    value = approved.get("sidecar_path")
    if not isinstance(value, str) or not Path(value).is_absolute():
        _fail("approved plan sidecar path is invalid")
    return Path(value)


def _fail(message: str) -> Never:
    raise IsolationError(message)
