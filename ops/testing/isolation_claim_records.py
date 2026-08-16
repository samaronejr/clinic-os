"""Parse closed claim specifications and build reserved ledger records."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import Final, Never, cast

from ops.testing.isolation_candidate_contract import (
    contract_for_purpose,
    validate_candidate_desired,
)
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    JsonValue,
    load_json,
    regular_identity,
)
from ops.testing.isolation_process_claim import (
    reserved_process_observed,
    validate_process_desired,
)
from ops.testing.isolation_stack_claim import (
    reserved_stack_observed,
    validate_stack_desired,
)
from ops.testing.isolation_todo_receipt_claim import validate_todo_receipt_desired

SPEC_KEYS: Final = frozenset(
    {"claim_id", "kind", "purpose", "dependency_claim_ids", "desired"}
)
FILESYSTEM_KEYS: Final = frozenset({"owned_files", "published_outputs"})
OWNED_FILE_KEYS: Final = frozenset({"relative_path", "mode", "uid", "gid", "sha256"})
UUID_PATTERN: Final = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
SHA256_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
CONTROL_CHARACTER_BOUNDARY: Final = 32


def _fail(message: str) -> Never:
    raise IsolationError(message)


def load_claim_spec(path: Path) -> JsonObject:
    """Load one canonical immutable closed claim specification."""
    if not path.is_absolute() or path.is_symlink():
        _fail("claim spec must be an absolute non-symlink file")
    regular_identity(path, mode=MODE_IMMUTABLE)
    spec, _ = load_json(path)
    if set(spec) != SPEC_KEYS:
        _fail("claim spec has the wrong closed key set")
    _claim_id(spec["claim_id"])
    kind = _text(spec["kind"], "claim kind")
    if kind not in {"filesystem", "process", "stack"}:
        _fail("claim kind is not implemented by this transition slice")
    purpose = _text(spec["purpose"], "claim purpose")
    if (
        not purpose.strip()
        or purpose != purpose.strip()
        or any(ord(item) < CONTROL_CHARACTER_BOUNDARY for item in purpose)
    ):
        _fail("claim purpose is not a nonblank canonical string")
    dependencies = _strings(spec["dependency_claim_ids"], "dependency claim IDs")
    if dependencies != sorted(set(dependencies)):
        _fail("dependency claim IDs are not sorted and unique")
    for dependency in dependencies:
        _claim_id(dependency)
    if spec["claim_id"] in dependencies:
        _fail("claim cannot depend on itself")
    desired = _object(spec["desired"], "claim desired")
    if kind == "filesystem":
        _validate_filesystem(desired, purpose, _text(spec["claim_id"], "claim ID"))
    elif kind == "stack":
        validate_stack_desired(desired)
    else:
        validate_process_desired(desired)
    return spec


def build_reserved_claim(spec: JsonObject, timestamp: str) -> JsonObject:
    """Project one validated spec into its exact reserved claim record."""
    claim_id = _text(spec["claim_id"], "claim ID")
    desired = _object(spec["desired"], "claim desired")
    return {
        "activated_at_utc": None,
        "candidate_envelope_binding": None,
        "claim_id": claim_id,
        "dependency_claim_ids": spec["dependency_claim_ids"],
        "desired": desired,
        "kind": spec["kind"],
        "last_verified_at_utc": timestamp,
        "observed": _reserved_observed(spec["kind"]),
        "prepared_at_utc": None,
        "purpose": spec["purpose"],
        "reserved_at_utc": timestamp,
        "root_relative_path": f"claims/{claim_id}",
        "runner_creation": None,
        "status": "reserved",
    }


def _reserved_observed(kind: JsonValue) -> JsonObject:
    if kind == "filesystem":
        return {"owned_files": [], "published_outputs": []}
    if kind == "stack":
        return reserved_stack_observed()
    if kind == "process":
        return reserved_process_observed()
    _fail("claim kind has no reserved observation")


def validate_dependencies(spec: JsonObject, claims: list[JsonObject]) -> None:
    """Require every new edge to target one active same-ledger claim."""
    claim_id = _text(spec["claim_id"], "claim ID")
    by_id: dict[str, JsonObject] = {}
    for claim in claims:
        existing_id = _text(claim.get("claim_id"), "existing claim ID")
        if existing_id in by_id:
            _fail("ledger contains a duplicate claim ID")
        by_id[existing_id] = claim
    if claim_id in by_id:
        _fail("claim ID is already reserved")
    dependencies = _strings(spec["dependency_claim_ids"], "dependency claim IDs")
    for dependency_id in dependencies:
        dependency = by_id.get(dependency_id)
        if dependency is None:
            _fail("dependency claim is missing")
        if dependency.get("status") != "active":
            _fail("dependency claim is not active")
        creation = dependency.get("runner_creation")
        if isinstance(creation, dict) and creation.get("state") != "prepared":
            _fail("runner dependency is not operational")


def validate_existing_dependencies(claim: JsonObject, claims: list[JsonObject]) -> None:
    """Revalidate every dependency of an existing claim before transition."""
    claim_id = _text(claim["claim_id"], "claim ID")
    dependencies = _strings(claim["dependency_claim_ids"], "dependency claim IDs")
    peers = [item for item in claims if item.get("claim_id") != claim_id]
    probe: JsonObject = {
        "claim_id": claim_id,
        "dependency_claim_ids": cast("JsonValue", dependencies),
    }
    validate_dependencies(probe, peers)


def validate_claim_id(value: JsonValue) -> str:
    """Return one canonical claim UUID or fail the transition boundary."""
    return _claim_id(value)


def claim_objects(value: JsonValue) -> list[JsonObject]:
    """Narrow a ledger claim array after the root parser checked its shape."""
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail("ledger claims are not objects")
    return cast("list[JsonObject]", value)


def validate_candidate_claim_root(spec: JsonObject, attempt_root: Path) -> None:
    """Bind candidate authorization roots to the current attempt."""
    contract = contract_for_purpose(spec.get("purpose"))
    if contract is None:
        if spec.get("purpose") == "todo-evidence-staging":
            desired = _object(spec.get("desired"), "todo desired")
            validate_todo_receipt_desired(desired, attempt_root)
        return
    desired = _object(spec.get("desired"), "candidate desired")
    claim_id = _text(spec.get("claim_id"), "claim ID")
    validate_candidate_desired(desired, claim_id, contract, attempt_root)


def _validate_filesystem(desired: JsonObject, purpose: str, claim_id: str) -> None:
    if set(desired) != FILESYSTEM_KEYS:
        _fail("filesystem desired has the wrong closed key set")
    files = _objects(desired["owned_files"], "owned files")
    _validate_owned_files(files)
    contract = contract_for_purpose(purpose)
    if contract is None:
        if purpose == "todo-evidence-staging":
            validate_todo_receipt_desired(desired, None)
            return
        if desired["published_outputs"] != []:
            _fail("published outputs are not implemented for this claim purpose")
        return
    validate_candidate_desired(desired, claim_id, contract, None)


def _validate_owned_files(files: list[JsonObject]) -> None:
    paths: list[str] = []
    for item in files:
        if set(item) != OWNED_FILE_KEYS:
            _fail("owned file has the wrong closed key set")
        relative = _text(item["relative_path"], "owned-file relative path")
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or str(path) != relative:
            _fail("owned file escaped the claim root")
        if item.get("mode") != MODE_PRIVATE:
            _fail("owned mutable staging file must use mode 0600")
        if item.get("uid") != os.geteuid() or item.get("gid") != os.getegid():
            _fail("owned file is not executor-owned")
        if (
            not isinstance(item.get("sha256"), str)
            or SHA256_PATTERN.fullmatch(cast("str", item["sha256"])) is None
        ):
            _fail("owned file SHA-256 is invalid")
        paths.append(relative)
    if paths != sorted(set(paths)):
        _fail("owned files are not sorted and unique")


def _claim_id(value: JsonValue) -> str:
    text = _text(value, "claim ID")
    if UUID_PATTERN.fullmatch(text) is None:
        _fail("claim ID is not a canonical UUID")
    return text


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} must be an object array")
    return cast("list[JsonObject]", value)


def _strings(value: JsonValue, context: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _fail(f"{context} must be a string array")
    return cast("list[str]", value)


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value
