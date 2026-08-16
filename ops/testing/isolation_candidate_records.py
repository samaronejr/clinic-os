"""Project candidate bindings and publication observations deterministically."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final, Never, cast

import rfc8785

from ops.testing.isolation_candidate_contract import (
    CandidateContract,
    envelope_binding,
    validate_candidate_envelope,
)
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    raw_sha256,
)

AUTHORIZATION_COUNT: Final = 2


def _fail(message: str) -> Never:
    raise IsolationError(message)


def bound_envelope_bytes(
    value: JsonValue,
    ledger: JsonObject,
    claim: JsonObject,
    contract: CandidateContract,
) -> tuple[JsonObject, bytes]:
    """Authenticate an immutable candidate binding and reconstruct its bytes."""
    binding = object_value(value, "candidate envelope binding")
    if set(binding) != {"schema_version", "envelope", "envelope_sha256"}:
        _fail("candidate envelope binding has the wrong closed shape")
    envelope = object_value(binding.get("envelope"), "bound candidate envelope")
    validate_candidate_envelope(envelope, ledger, claim, contract)
    if binding != envelope_binding(envelope):
        _fail("candidate envelope binding hash drifted")
    return envelope, rfc8785.dumps(envelope) + b"\n"


def candidate_observations(
    claim: JsonObject,
    authorizations: list[JsonObject],
) -> list[JsonObject]:
    """Validate the two ordered candidate authorization observation identities."""
    observed = object_value(claim["observed"], "candidate observed")
    if observed.get("owned_files") != []:
        _fail("candidate publisher has unexpected active staging identities")
    outputs = object_values(observed.get("published_outputs"), "candidate observations")
    if len(outputs) != len(authorizations):
        _fail("candidate publisher has the wrong observation count")
    identity_keys = {
        "authorization_id",
        "governing_lock",
        "output_kind",
        "root_path",
    }
    for authorization, output in zip(authorizations, outputs, strict=True):
        base = unpublished_observation(authorization)
        if any(output.get(key) != base[key] for key in identity_keys):
            _fail("candidate observation identity drifted")
        if output.get("status") not in {"unpublished", "published"}:
            _fail("candidate observation status is invalid")
    return outputs


def unpublished_observation(authorization: JsonObject) -> JsonObject:
    """Project one exact empty candidate authorization observation."""
    return {
        "authorization_id": authorization["authorization_id"],
        "entries": [],
        "governing_lock": authorization["governing_lock"],
        "output_kind": authorization["output_kind"],
        "root_path": authorization["root_path"],
        "status": "unpublished",
    }


def published_observation(authorization: JsonObject, raw: bytes) -> JsonObject:
    """Project one exact completed one-file candidate authorization."""
    relative_paths = authorization["relative_paths"]
    if not isinstance(relative_paths, list) or len(relative_paths) != 1:
        _fail("candidate authorization path count drifted")
    return {
        "authorization_id": authorization["authorization_id"],
        "entries": [
            {
                "gid": authorization["gid"],
                "mode": authorization["mode"],
                "relative_path": relative_paths[0],
                "sha256": raw_sha256(raw),
                "size_bytes": len(raw),
                "uid": authorization["uid"],
            }
        ],
        "governing_lock": authorization["governing_lock"],
        "output_kind": authorization["output_kind"],
        "root_path": authorization["root_path"],
        "status": "published",
    }


def authorization_destination(authorization: JsonObject) -> Path:
    """Resolve the sole authorized relative path beneath its canonical root."""
    root = Path(text_value(authorization["root_path"], "authorization root"))
    paths = authorization["relative_paths"]
    if not isinstance(paths, list) or len(paths) != 1 or not isinstance(paths[0], str):
        _fail("candidate authorization requires one relative path")
    return root / paths[0]


def candidate_history_record(
    ledger: JsonObject,
    claim: JsonObject,
    binding: JsonObject,
    authorizations: list[JsonObject],
    envelope_observation: JsonObject,
) -> JsonObject:
    """Build the timestamp-free predecessor- and binding-bound history tombstone."""
    predecessor_entries = envelope_observation.get("entries")
    if not isinstance(predecessor_entries, list):
        _fail("candidate predecessor entries are invalid")
    if len(authorizations) != AUTHORIZATION_COUNT:
        _fail("candidate history requires the exact authorization pair")
    envelope_authorization, history_authorization = authorizations
    history_paths = history_authorization.get("relative_paths")
    if (
        not isinstance(history_paths, list)
        or len(history_paths) != 1
        or not isinstance(history_paths[0], str)
    ):
        _fail("candidate history authorization path is invalid")
    return {
        "attempt_id": ledger["attempt_id"],
        "authorization_id": history_authorization["authorization_id"],
        "candidate_envelope_binding_sha256": hashlib.sha256(
            rfc8785.dumps(binding)
        ).hexdigest(),
        "claim_id": claim["claim_id"],
        "output_kind": "publication-history",
        "predecessor_authorization_id": envelope_authorization["authorization_id"],
        "predecessor_entries_sha256": hashlib.sha256(
            rfc8785.dumps(predecessor_entries)
        ).hexdigest(),
        "predecessor_root_path": envelope_authorization["root_path"],
        "purpose": claim["purpose"],
        "relative_path": history_paths[0],
        "root_path": history_authorization["root_path"],
        "schema_version": 1,
    }


def object_value(value: JsonValue, context: str) -> JsonObject:
    """Narrow a validated JSON value to an object at a security boundary."""
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def object_values(value: JsonValue, context: str) -> list[JsonObject]:
    """Narrow a validated JSON value to an object array."""
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} must be an object array")
    return cast("list[JsonObject]", value)


def text_value(value: JsonValue, context: str) -> str:
    """Narrow a validated JSON value to text."""
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value
