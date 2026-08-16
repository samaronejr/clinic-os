"""Publish one immutable manifest after final input authentication."""

from __future__ import annotations

import copy
import re
from typing import TYPE_CHECKING, Final, Never

from ops.testing.isolation_atomic_publication import (
    publish_immutable,
    require_published_bytes,
)
from ops.testing.isolation_candidate_records import (
    authorization_destination,
    published_observation,
    unpublished_observation,
)
from ops.testing.isolation_claim_records import claim_objects, validate_claim_id
from ops.testing.isolation_common import (
    MODE_DIRECTORY,
    IsolationError,
    JsonObject,
    canonical_bytes,
    directory_identity,
    raw_sha256,
)
from ops.testing.isolation_final_input_auth import (
    REQUIRED_SUITES,
    CandidateEvidence,
    CandidateInput,
    authenticate_candidate,
    authenticate_probe,
    authenticate_receipts,
    immutable_json,
    immutable_raw,
    prepublication_ledger_sha,
    validate_bound_file,
    validate_freeze_ledger,
)
from ops.testing.isolation_final_input_auth import (
    CandidateInspection as _CandidateInspection,
)
from ops.testing.isolation_final_input_auth import (
    FinalInputFreeze as _FinalInputFreeze,
)
from ops.testing.isolation_final_input_auth import (
    SourceInspection as _SourceInspection,
)
from ops.testing.isolation_ledger_store import LedgerSession, locked_open_ledger
from ops.testing.isolation_lineage import load_complete_lineage
from ops.testing.isolation_terminal_publisher_authorizations import (
    entry_present,
    publisher_input_state,
    validate_terminal_publisher_claim,
)

if TYPE_CHECKING:
    from pathlib import Path

SHA40: Final = re.compile(r"^[0-9a-f]{40}$")


def _freeze_final_inputs(request: _FinalInputFreeze) -> Path:
    _validate_request(request)
    with locked_open_ledger(request.ledger_path) as session:
        manifest, publisher = _authenticated_manifest(request, session)
        _publish_manifest(session, publisher, (canonical_bytes(manifest), request))
    return request.output_path


def _authenticated_manifest(
    request: _FinalInputFreeze, session: LedgerSession
) -> tuple[JsonObject, JsonObject]:
    ledger = session.ledger
    validate_freeze_ledger(request, ledger)
    plan_raw = immutable_raw(request.approved_plan, "approved plan")
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
    manifest: JsonObject = {
        "application_candidate": application.manifest,
        "approved_plan_sha256": raw_sha256(plan_raw),
        "attempt_id": request.attempt_id,
        "execution_host_preflight_sha256": raw_sha256(proof_raw),
        "final_input_probe_sha256": authenticate_probe(
            request.final_probe_path, "final-input-freeze", proof_raw
        ),
        "final_required_browser_suites": list(REQUIRED_SUITES),
        "kickoff_probe_sha256": authenticate_probe(
            request.kickoff_probe_path, "todo1-kickoff", proof_raw
        ),
        "ledger_sha256": prepublication_ledger_sha(session, publisher),
        "lineage_validation_sha256": lineage.validation_sha256,
        "primary_receipts": authenticate_receipts(request.receipt_root),
        "receipt_lineage_sha256": lineage.lineage_sha256,
        "runner_candidate": runner.manifest,
        "schema_version": 1,
        "sha": request.sha,
        "shared_evidence_baseline_sha256": raw_sha256(baseline_raw),
        "terminal_publisher_claim_id": publisher["claim_id"],
        "tree_sha": request.tree_sha,
    }
    return manifest, publisher


def _candidate(
    request: _FinalInputFreeze, ledger: JsonObject, kind: str
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


def _validate_request(request: _FinalInputFreeze) -> None:
    roots = (
        request.worktree,
        request.attempt_root,
        request.staging_root,
        request.output_path,
    )
    if not all(path.is_absolute() for path in roots):
        _fail("final input paths must be absolute")
    if request.staging_root.parent != request.attempt_root / "claims":
        _fail("final input staging root is not claim-owned")
    expected_output = (
        request.attempt_root.parents[1]
        / "clinic-os-phase1a-final"
        / "terminal"
        / "inputs.json"
    )
    if (
        request.attempt_root.name != request.attempt_id
        or request.attempt_root.parent.name != "clinic-os-phase1a-runtime"
        or request.output_path != expected_output
    ):
        _fail("final input destination is not the fixed terminal path")
    try:
        for path in (
            request.attempt_root,
            request.staging_root,
            request.output_path.parent,
        ):
            if directory_identity(path).get("mode") != MODE_DIRECTORY:
                _fail("final input directory is not private")
    except FileNotFoundError as error:
        message = "final input publisher directory is absent"
        raise IsolationError(message) from error
    inspected = request.source_inspection
    if (
        SHA40.fullmatch(request.sha) is None
        or SHA40.fullmatch(request.tree_sha) is None
        or (inspected.sha, inspected.tree_sha, inspected.clean)
        != (request.sha, request.tree_sha, True)
    ):
        _fail("final input source inspection drifted")


def _publisher_gate(
    request: _FinalInputFreeze,
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


def _publish_manifest(
    session: LedgerSession,
    publisher: JsonObject,
    publication: tuple[bytes, _FinalInputFreeze],
) -> None:
    raw, request = publication
    authorization, observation = publisher_input_state(publisher)
    destination = authorization_destination(authorization)
    if destination != request.output_path:
        _fail("terminal publisher input destination drifted")
    expected = published_observation(authorization, raw)
    if observation.get("status") == "published":
        if observation != expected:
            _fail("terminal publisher input observation drifted")
        _ = validate_terminal_publisher_claim(publisher, destination.parent)
        require_published_bytes(destination, raw)
        return
    if observation != unpublished_observation(authorization):
        _fail("terminal publisher input observation is not unpublished")
    if not entry_present(destination):
        _ = validate_terminal_publisher_claim(publisher, destination.parent)
    publish_manifest_bytes(destination, raw, request.attempt_id)
    projected = copy.deepcopy(publisher)
    _authorization, projected_observation = publisher_input_state(projected)
    projected_observation.clear()
    projected_observation.update(expected)
    _ = validate_terminal_publisher_claim(projected, destination.parent)
    observation.clear()
    observation.update(expected)
    session.commit()
    _ = validate_terminal_publisher_claim(publisher, destination.parent)


def _fail(message: str) -> Never:
    raise IsolationError(message)


CandidateInspection = _CandidateInspection
SourceInspection = _SourceInspection
FinalInputFreeze = _FinalInputFreeze
freeze_final_inputs = _freeze_final_inputs
publish_manifest_bytes = publish_immutable
