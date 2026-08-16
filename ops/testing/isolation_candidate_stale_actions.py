"""Derive changed-boot candidate publication actions from a prior claim."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Never

import rfc8785

from ops.testing.isolation_candidate_contract import (
    contract_for_purpose,
    validate_candidate_desired,
)
from ops.testing.isolation_candidate_records import (
    bound_envelope_bytes,
    candidate_history_record,
    candidate_observations,
    object_value,
    object_values,
    published_observation,
    text_value,
    unpublished_observation,
)
from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue


def _fail(message: str) -> Never:
    raise IsolationError(message)


@dataclass(frozen=True, slots=True)
class _IdentityInputs:
    ledger: JsonObject
    claim: JsonObject
    binding: JsonObject
    authorizations: list[JsonObject]
    envelope_observation: JsonObject
    envelope_raw: bytes
    history: JsonObject
    history_observation: JsonObject


def build_candidate_stale_actions(
    ledger: JsonObject,
    claim: JsonObject,
) -> tuple[list[JsonObject], list[JsonObject]]:
    """Return ordered action summaries and their exact hashed identity objects."""
    contract = contract_for_purpose(claim.get("purpose"))
    if contract is None:
        _fail("stale candidate action source has the wrong purpose")
    attempt_root = Path(text_value(ledger.get("attempt_root"), "attempt root"))
    claim_id = text_value(claim.get("claim_id"), "candidate claim ID")
    desired = object_value(claim.get("desired"), "candidate desired")
    validate_candidate_desired(desired, claim_id, contract, attempt_root)
    authorizations = object_values(
        desired["published_outputs"], "candidate authorizations"
    )
    observations = candidate_observations(claim, authorizations)
    binding_value = claim.get("candidate_envelope_binding")
    if binding_value is None:
        return [], []
    if claim.get("status") != "active":
        _fail("bound stale candidate is not active")
    binding = object_value(binding_value, "candidate envelope binding")
    _, envelope_raw = bound_envelope_bytes(binding, ledger, claim, contract)
    envelope_observation = published_observation(authorizations[0], envelope_raw)
    history = candidate_history_record(
        ledger,
        claim,
        binding,
        authorizations,
        envelope_observation,
    )
    history_raw = rfc8785.dumps(history) + b"\n"
    history_observation = published_observation(authorizations[1], history_raw)
    if observations[1] == history_observation:
        if observations[0] != envelope_observation:
            _fail("published candidate history lacks its envelope predecessor")
        return [], []
    if observations[1] != unpublished_observation(authorizations[1]):
        _fail("stale candidate history observation drifted")
    if observations[0] != envelope_observation and observations[
        0
    ] != unpublished_observation(authorizations[0]):
        _fail("stale candidate envelope observation drifted")
    identities = _identity_chain(
        _IdentityInputs(
            ledger,
            claim,
            binding,
            authorizations,
            envelope_observation,
            envelope_raw,
            history,
            history_observation,
        )
    )
    return [_action_summary(identity) for identity in identities], identities


def _identity_chain(inputs: _IdentityInputs) -> list[JsonObject]:
    ledger = inputs.ledger
    claim = inputs.claim
    binding = inputs.binding
    authorizations = inputs.authorizations
    envelope_observation = inputs.envelope_observation
    envelope_raw = inputs.envelope_raw
    history = inputs.history
    history_observation = inputs.history_observation
    claim_id = text_value(claim["claim_id"], "candidate claim ID")
    attempt_id = ledger["attempt_id"]
    envelope_id = f"candidate-envelope-complete-{claim_id}"
    staging_id = f"candidate-staging-complete-{claim_id}"
    history_id = f"candidate-history-complete-{claim_id}"
    origin_sha = _digest(claim)
    binding_sha = _digest(binding)
    envelope_auth, history_auth = authorizations
    envelope_identity: JsonObject = {
        "action_id": envelope_id,
        "attempt_id": attempt_id,
        "authorization_id": envelope_auth["authorization_id"],
        "candidate_envelope_binding": binding,
        "candidate_envelope_binding_sha256": binding_sha,
        "claim_id": claim_id,
        "operation": "publish-or-adopt-candidate-envelope",
        "origin_claim_sha256": origin_sha,
        "relative_path": _relative_path(envelope_auth),
        "resource_kind": "candidate-publication-envelope",
        "root_path": envelope_auth["root_path"],
        "schema_version": 1,
        "would_be_observed_authorization": envelope_observation,
    }
    claim_root = Path(text_value(ledger["attempt_root"], "attempt root"))
    claim_root /= text_value(claim["root_relative_path"], "claim root")
    staging_identity: JsonObject = {
        "action_id": staging_id,
        "attempt_id": attempt_id,
        "candidate_envelope_binding_sha256": binding_sha,
        "claim_id": claim_id,
        "claim_root_path": str(claim_root),
        "expected_staged_entry": {
            "gid": envelope_auth["gid"],
            "mode": 0o400,
            "relative_path": "candidate-envelope.json",
            "sha256": binding["envelope_sha256"],
            "size_bytes": len(envelope_raw),
            "uid": envelope_auth["uid"],
        },
        "operation": "remove-staging",
        "origin_claim_sha256": origin_sha,
        "predecessor_action_id": envelope_id,
        "relative_path": "candidate-envelope.json",
        "resource_kind": "filesystem-staging",
        "schema_version": 1,
    }
    history_identity: JsonObject = {
        "action_id": history_id,
        "attempt_id": attempt_id,
        "authorization_id": history_auth["authorization_id"],
        "candidate_envelope_binding_sha256": binding_sha,
        "claim_id": claim_id,
        "envelope_action_id": envelope_id,
        "history_record": history,
        "operation": "publish-or-adopt-candidate-history",
        "origin_claim_sha256": origin_sha,
        "predecessor_action_id": staging_id,
        "predecessor_authorization_id": envelope_auth["authorization_id"],
        "predecessor_entries_sha256": history["predecessor_entries_sha256"],
        "predecessor_root_path": envelope_auth["root_path"],
        "relative_path": _relative_path(history_auth),
        "resource_kind": "candidate-publication-history",
        "root_path": history_auth["root_path"],
        "schema_version": 1,
        "would_be_observed_authorization": history_observation,
    }
    return [envelope_identity, staging_identity, history_identity]


def _action_summary(identity: JsonObject) -> JsonObject:
    return {
        "action_id": identity["action_id"],
        "claim_id": identity["claim_id"],
        "identity_sha256": _digest(identity),
        "operation": identity["operation"],
        "resource_kind": identity["resource_kind"],
    }


def _relative_path(authorization: JsonObject) -> str:
    paths = authorization.get("relative_paths")
    if not isinstance(paths, list) or len(paths) != 1 or not isinstance(paths[0], str):
        _fail("candidate action authorization path is invalid")
    return paths[0]


def _digest(value: JsonValue) -> str:
    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()
