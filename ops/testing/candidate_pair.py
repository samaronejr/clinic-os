"""Validate released candidate envelope and publication-history pairs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

import rfc8785

from ops.testing.isolation_candidate_contract import envelope_binding
from ops.testing.isolation_service_contracts import validate_service_contracts

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject, JsonValue


ENVELOPE_KEYS: Final = frozenset(
    {
        "attempt_id",
        "authorization_id",
        "claim_id",
        "image_contract",
        "image_id",
        "published_at_utc",
        "revision_sha",
        "schema_version",
        "tree_sha",
    }
)
HISTORY_KEYS: Final = frozenset(
    {
        "attempt_id",
        "authorization_id",
        "candidate_envelope_binding_sha256",
        "claim_id",
        "output_kind",
        "predecessor_authorization_id",
        "predecessor_entries_sha256",
        "predecessor_root_path",
        "purpose",
        "relative_path",
        "root_path",
        "schema_version",
    }
)


def validate_candidate_pair(
    envelope_raw: bytes,
    history_raw: bytes,
    ledger_claims: list[JsonObject],
) -> JsonObject:
    """Reconstruct the binding and require the publisher UUID to be absent."""
    envelope = _canonical_object(envelope_raw)
    history = _canonical_object(history_raw)
    claim_id = envelope.get("claim_id")
    image = _object(envelope.get("image_contract"))
    kind = image.get("kind")
    validate_service_contracts(image, None)
    prefix = (
        "candidate-application" if kind == "application" else "candidate-browser-runner"
    )
    binding = envelope_binding(envelope)
    if (
        set(envelope) != ENVELOPE_KEYS
        or set(history) != HISTORY_KEYS
        or envelope.get("schema_version") != 1
        or history.get("schema_version") != 1
        or envelope.get("authorization_id") != f"{prefix}-envelope"
        or history.get("authorization_id") != f"{prefix}-publication-history"
        or history.get("predecessor_authorization_id") != f"{prefix}-envelope"
        or history.get("purpose") != f"{prefix}-publisher"
        or history.get("output_kind") != "publication-history"
        or history.get("attempt_id") != envelope.get("attempt_id")
        or history.get("claim_id") != claim_id
        or history.get("relative_path") != f"{claim_id}.json"
        or history.get("candidate_envelope_binding_sha256")
        != hashlib.sha256(rfc8785.dumps(binding)).hexdigest()
        or not _absolute(history.get("root_path"))
        or not _absolute(history.get("predecessor_root_path"))
        or any(claim.get("claim_id") == claim_id for claim in ledger_claims)
    ):
        _fail()
    return envelope


def _canonical_object(raw: bytes) -> JsonObject:
    value: JsonValue = json.loads(raw)
    if not isinstance(value, dict) or raw != rfc8785.dumps(value) + b"\n":
        _fail()
    return _object(value)


def _object(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        _fail()
    return value


def _absolute(value: object) -> bool:
    return isinstance(value, str) and Path(value).is_absolute()


def _fail() -> Never:
    message = "candidate pair contract failed"
    raise _CandidatePairError(message)


class _CandidatePairError(RuntimeError):
    pass
