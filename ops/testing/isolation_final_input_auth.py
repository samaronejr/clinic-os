"""Authenticate immutable evidence consumed by the final input freezer."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Never

from ops.testing import isolation_final_suites as final_suites
from ops.testing.cgroup_probe_contract import validate_probe_journal
from ops.testing.isolation_candidate_contract import (
    candidate_desired,
    contract_for_purpose,
    envelope_binding,
    validate_candidate_desired,
    validate_candidate_envelope,
)
from ops.testing.isolation_candidate_publication import (
    validate_candidate_publication_time,
)
from ops.testing.isolation_candidate_records import (
    authorization_destination,
    candidate_history_record,
    object_values,
    published_observation,
    unpublished_observation,
)
from ops.testing.isolation_claim_records import claim_objects, validate_claim_id
from ops.testing.isolation_common import (
    MAX_JSON_BYTES,
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
    raw_sha256,
    regular_identity,
)
from ops.testing.isolation_terminal_publisher_authorizations import (
    publisher_input_state,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ops.testing.isolation_ledger_store import LedgerSession

REQUIRED_SUITES = final_suites.REQUIRED_SUITES


@dataclass(frozen=True, slots=True)
class _CandidateInspection:
    image_id: str
    image_contract: JsonObject


@dataclass(frozen=True, slots=True)
class _CandidateInput:
    kind: str
    envelope_path: Path
    history_path: Path
    inspection: _CandidateInspection


@dataclass(frozen=True, slots=True)
class _SourceInspection:
    sha: str
    tree_sha: str
    clean: bool


@dataclass(frozen=True, slots=True)
class _FinalInputFreeze:
    worktree: Path
    sha: str
    tree_sha: str
    attempt_id: str
    attempt_root: Path
    approved_plan: Path
    proof_path: Path
    ledger_path: Path
    shared_baseline_path: Path
    kickoff_probe_path: Path
    final_probe_path: Path
    receipt_root: Path
    application_envelope_path: Path
    application_history_path: Path
    runner_envelope_path: Path
    runner_history_path: Path
    application_inspection: _CandidateInspection
    runner_inspection: _CandidateInspection
    staging_root: Path
    output_path: Path
    source_inspection: _SourceInspection
    codex_source_path: Path
    uv_source_path: Path
    final_suite_path: Path


@dataclass(frozen=True, slots=True)
class _CandidateEvidence:
    claim_id: str
    manifest: JsonObject


def _authenticate_candidate(
    request: _FinalInputFreeze,
    ledger: JsonObject,
    candidate: _CandidateInput,
) -> _CandidateEvidence:
    kind, inspection = candidate.kind, candidate.inspection
    envelope, envelope_raw = _immutable_json(
        candidate.envelope_path, f"{kind} envelope"
    )
    history, history_raw = _immutable_json(candidate.history_path, f"{kind} history")
    purpose = f"candidate-{kind}-publisher"
    contract = contract_for_purpose(purpose)
    if contract is None:
        _fail("candidate input kind is invalid")
    try:
        claim_id = validate_claim_id(envelope.get("claim_id"))
    except IsolationError as error:
        message = f"{kind} candidate claim ID is invalid"
        raise IsolationError(message) from error
    desired = candidate_desired(request.attempt_root, claim_id, envelope)
    claim: JsonObject = {"claim_id": claim_id, "desired": desired, "purpose": purpose}
    validate_candidate_desired(desired, claim_id, contract, request.attempt_root)
    validate_candidate_envelope(envelope, ledger, claim, contract)
    validate_candidate_publication_time(
        envelope, ledger.get("created_at_utc"), ledger.get("last_verified_at_utc")
    )
    if (
        envelope.get("revision_sha") != request.sha
        or envelope.get("tree_sha") != request.tree_sha
        or envelope.get("image_id") != inspection.image_id
        or envelope.get("image_contract") != inspection.image_contract
    ):
        _fail(f"{kind} candidate identity drifted")
    authorizations = object_values(desired["published_outputs"], "authorizations")
    binding = envelope_binding(envelope)
    observation = published_observation(authorizations[0], envelope_raw)
    expected_history = candidate_history_record(
        ledger, claim, binding, authorizations, observation
    )
    if history != expected_history:
        _fail(f"{kind} candidate history projection drifted")
    if candidate.envelope_path != authorization_destination(
        authorizations[0]
    ) or candidate.history_path != authorization_destination(authorizations[1]):
        _fail(f"{kind} candidate publication path drifted")
    suites = inspection.image_contract.get("available_suite_ids")
    if kind == "browser-runner" and suites != list(REQUIRED_SUITES):
        _fail("browser-runner candidate lacks the exact final suite set")
    manifest: JsonObject = {
        "claim_id": claim_id,
        "envelope_path": str(candidate.envelope_path),
        "envelope_sha256": raw_sha256(envelope_raw),
        "history_path": str(candidate.history_path),
        "history_sha256": raw_sha256(history_raw),
        "image_contract": inspection.image_contract,
        "image_id": inspection.image_id,
    }
    return _CandidateEvidence(claim_id, manifest)


def _authenticate_receipts(root: Path) -> list[JsonValue]:
    expected = [
        f"task-{todo}-clinic-os-phase-1a-staff-scheduling.json" for todo in range(1, 21)
    ]
    if sorted(path.name for path in root.iterdir()) != sorted(expected):
        _fail("final input receipt set is not exactly twenty primary receipts")
    entries: list[JsonValue] = []
    for todo, name in enumerate(expected, 1):
        receipt, raw = _immutable_json(root / name, "todo receipt")
        if receipt.get("schema_version") != 1 or receipt.get("todo") != todo:
            _fail("final input receipt identity is invalid")
        entries.append({"relative_path": name, "sha256": raw_sha256(raw), "todo": todo})
    return entries


def _authenticate_probe(path: Path, purpose: str, proof_raw: bytes) -> str:
    try:
        probe, raw = _immutable_json(path, f"{purpose} probe")
    except FileNotFoundError as error:
        message = f"required {purpose} probe is absent"
        raise IsolationError(message) from error
    validate_probe_journal(probe)
    if (
        probe.get("purpose") != purpose
        or probe.get("state") != "removed"
        or probe.get("proof_sha256") != raw_sha256(proof_raw)
    ):
        _fail(f"{purpose} probe is incomplete or bound to another proof")
    return raw_sha256(raw)


def _validate_freeze_ledger(request: _FinalInputFreeze, ledger: JsonObject) -> None:
    if (
        ledger.get("attempt_id") != request.attempt_id
        or ledger.get("state") != "open"
        or ledger.get("attempt_root") != str(request.attempt_root)
        or ledger.get("worktree_realpath") != str(request.worktree.resolve())
    ):
        _fail("final input ledger identity or state drifted")


def _validate_bound_file(value: JsonValue, path: Path, raw: bytes) -> None:
    if (
        not isinstance(value, dict)
        or value.get("path") != str(path)
        or value.get("sha256") != raw_sha256(raw)
    ):
        _fail("ledger immutable-file binding drifted")


def _prepublication_ledger_sha(session: LedgerSession, publisher: JsonObject) -> str:
    authorization, observation = publisher_input_state(publisher)
    unpublished = unpublished_observation(authorization)
    if observation == unpublished:
        return raw_sha256(session.original_raw)
    if observation.get("status") != "published":
        _fail("terminal publisher input observation state is invalid")
    previous = copy.deepcopy(session.ledger)
    matches = [
        item
        for item in claim_objects(previous["claims"])
        if item.get("claim_id") == publisher.get("claim_id")
    ]
    if len(matches) != 1:
        _fail("terminal publisher claim identity drifted")
    _authorization, prior_observation = publisher_input_state(matches[0])
    prior_observation.clear()
    prior_observation.update(unpublished)
    return raw_sha256(canonical_bytes(previous))


def _immutable_json(path: Path, context: str) -> tuple[JsonObject, bytes]:
    try:
        regular_identity(path, mode=MODE_IMMUTABLE)
        return load_json(path)
    except FileNotFoundError as error:
        message = f"required {context} is absent"
        raise IsolationError(message) from error


def _immutable_raw(path: Path, context: str) -> bytes:
    try:
        regular_identity(path, mode=MODE_IMMUTABLE)
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError as error:
        message = f"required {context} is absent"
        raise IsolationError(message) from error
    try:
        raw = b""
        while len(raw) <= MAX_JSON_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_JSON_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(descriptor)
    if len(raw) > MAX_JSON_BYTES:
        _fail(f"required {context} exceeds the bounded input size")
    return raw


def _fail(message: str) -> Never:
    raise IsolationError(message)


CandidateInspection = _CandidateInspection
CandidateInput = _CandidateInput
CandidateEvidence = _CandidateEvidence
SourceInspection = _SourceInspection
FinalInputFreeze = _FinalInputFreeze
authenticate_candidate = _authenticate_candidate
authenticate_receipts = _authenticate_receipts
authenticate_probe = _authenticate_probe
validate_freeze_ledger = _validate_freeze_ledger
validate_bound_file = _validate_bound_file
prepublication_ledger_sha = _prepublication_ledger_sha
immutable_json = _immutable_json
immutable_raw = _immutable_raw
