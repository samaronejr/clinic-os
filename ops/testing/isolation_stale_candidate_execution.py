"""Execute binding-derived changed-boot candidate publication actions."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
from typing import Never

import rfc8785

from ops.testing.isolation_atomic_publication import publish_immutable
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    JsonValue,
    ensure_private_directory,
    raw_sha256,
)


def execute_candidate_publication(identity: JsonObject) -> None:
    """Publish or byte-adopt the exact record bound into one stale identity."""
    operation = identity.get("operation")
    if operation == "publish-or-adopt-candidate-envelope":
        binding = _object(
            identity.get("candidate_envelope_binding"), "candidate binding"
        )
        if _digest(binding) != identity.get("candidate_envelope_binding_sha256"):
            _fail("candidate binding hash drifted")
        record = _object(binding.get("envelope"), "bound candidate envelope")
        raw = rfc8785.dumps(record) + b"\n"
        if raw_sha256(raw) != binding.get("envelope_sha256"):
            _fail("candidate envelope bytes differ from their binding")
    elif operation == "publish-or-adopt-candidate-history":
        record = _object(identity.get("history_record"), "candidate history")
        raw = rfc8785.dumps(record) + b"\n"
    else:
        _fail("candidate publication operation is invalid")
    destination = _destination(identity)
    _require_expected_entry(identity, raw, destination)
    _ensure_parent(destination, _absolute_path(identity.get("root_path"), "root"))
    publish_immutable(destination, raw, _text(identity.get("claim_id"), "claim ID"))


def _require_expected_entry(
    identity: JsonObject,
    raw: bytes,
    destination: Path,
) -> None:
    observation = _object(
        identity.get("would_be_observed_authorization"), "candidate observation"
    )
    entries = observation.get("entries")
    if not isinstance(entries, list) or len(entries) != 1:
        _fail("candidate observation requires one entry")
    entry = _object(entries[0], "candidate entry")
    expected = {
        "gid": os.getegid(),
        "mode": MODE_IMMUTABLE,
        "relative_path": identity.get("relative_path"),
        "sha256": raw_sha256(raw),
        "size_bytes": len(raw),
        "uid": os.geteuid(),
    }
    if entry != expected:
        _fail("candidate publication entry differs from bound bytes")
    if observation.get("status") != "published":
        _fail("candidate publication observation is not completed")
    root = _absolute_path(identity.get("root_path"), "root")
    if observation.get("root_path") != str(root) or destination != root / str(
        entry["relative_path"]
    ):
        _fail("candidate publication root or relative path drifted")


def _destination(identity: JsonObject) -> Path:
    root = _absolute_path(identity.get("root_path"), "root")
    raw = _text(identity.get("relative_path"), "relative path")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or str(relative) != raw or ".." in relative.parts:
        _fail("candidate publication path escapes its root")
    return root / raw


def _ensure_parent(destination: Path, root: Path) -> None:
    ensure_private_directory(root)
    relative_parent = destination.parent.relative_to(root)
    current = root
    for part in relative_parent.parts:
        current /= part
        ensure_private_directory(current)


def _absolute_path(value: JsonValue, context: str) -> Path:
    text = _text(value, context)
    path = Path(text)
    if not path.is_absolute():
        _fail(f"candidate {context} is not absolute")
    return path


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _digest(value: JsonValue) -> str:
    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()


def _fail(message: str) -> Never:
    raise IsolationError(message)
