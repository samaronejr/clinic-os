"""Authenticate the persistent final-terminal publisher authorization vector."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Never, cast

import rfc8785

from ops.testing.isolation_claim_records import validate_claim_id
from ops.testing.isolation_common import (
    MODE_DIRECTORY,
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    JsonValue,
    directory_identity,
)
from ops.testing.isolation_terminal_output_files import (
    canonical_relative_path,
    validate_output_observation,
)

AUTHORIZATION_KEYS: Final = {
    "authorization_id",
    "gid",
    "governing_lock",
    "mode",
    "output_kind",
    "predecessor_authorization_ids",
    "relative_paths",
    "root_path",
    "uid",
}
CLAIM_KEYS: Final = frozenset(
    {
        "activated_at_utc",
        "candidate_envelope_binding",
        "claim_id",
        "dependency_claim_ids",
        "desired",
        "kind",
        "last_verified_at_utc",
        "observed",
        "prepared_at_utc",
        "purpose",
        "reserved_at_utc",
        "root_relative_path",
        "runner_creation",
        "status",
    }
)
TIMESTAMP: Final = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)


@dataclass(frozen=True, slots=True)
class _AuthorizationValidation:
    claim: JsonObject
    authorizations: list[JsonObject]
    observed: JsonObject
    root: Path


AUTHORIZATIONS: Final = (
    ("input-launcher-path", (), ("F3-launcher.path",)),
    ("input-launcher-lstat", ("input-launcher-path",), ("F3-launcher.lstat",)),
    ("input-launcher-sha256", ("input-launcher-lstat",), ("F3-launcher.sha256",)),
    (
        "input-manifest",
        ("input-launcher-lstat", "input-launcher-path", "input-launcher-sha256"),
        ("inputs.json",),
    ),
    ("input-sidecar", ("input-manifest",), ("inputs.sha256",)),
    ("f1-verdict", (), ("F1-verdict.json",)),
    ("f1-receipt", ("f1-verdict",), ("F1-receipt.txt",)),
    ("f2-prerequisites", (), ("F2-prerequisites.json",)),
    ("f2-verdict", ("f2-prerequisites",), ("F2-verdict.json",)),
    ("f2-receipt", ("f2-verdict",), ("F2-receipt.txt",)),
    ("f3-artifacts", (), None),
    ("f3-receipt", ("f3-artifacts",), ("F3/receipt.json",)),
    ("f3-manifest", ("f3-receipt",), ("F3/manifest.json",)),
    ("f4-pre", (), ("F4-pre.txt",)),
    ("f4-final", ("f4-pre",), ("F4-final.txt",)),
)


def _publisher_input_state(claim: JsonObject) -> tuple[JsonObject, JsonObject]:
    try:
        desired = _object(claim["desired"], "publisher desired")
        observed = _object(claim["observed"], "publisher observed")
        authorizations = _objects(desired["published_outputs"], "authorizations")
        observations = _objects(observed["published_outputs"], "observations")
        authorization, observation = authorizations[3], observations[3]
    except (IndexError, KeyError, IsolationError) as error:
        message = "terminal publisher input authorization is incomplete"
        raise IsolationError(message) from error
    if (
        authorization.get("authorization_id") != "input-manifest"
        or observation.get("authorization_id") != "input-manifest"
    ):
        _fail("terminal publisher input authorization identity drifted")
    return authorization, observation


def _entry_present(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def validate_terminal_publisher_claim(
    claim: JsonObject,
    terminal_root: Path,
) -> str:
    """Validate one reserved or active publisher and hash its state vector."""
    _validate_claim_header(claim)
    identity = directory_identity(terminal_root)
    if identity.get("mode") != MODE_DIRECTORY:
        _fail("terminal publisher root is not the private terminal directory")
    desired = _object(claim.get("desired"), "publisher desired")
    if set(desired) != {"owned_files", "published_outputs"}:
        _fail("terminal publisher desired shape is open")
    if desired.get("owned_files") != []:
        _fail("terminal publisher cannot own mutable staging files")
    authorizations = _objects(desired.get("published_outputs"), "authorizations")
    _validate_authorizations(authorizations, terminal_root)
    observed = _object(claim.get("observed"), "publisher observation")
    if set(observed) != {"owned_files", "published_outputs"}:
        _fail("terminal publisher observation shape is open")
    if observed.get("owned_files") != []:
        _fail("terminal publisher observed mutable staging")
    validation = _AuthorizationValidation(
        claim, authorizations, observed, terminal_root
    )
    vector = _authorization_vector(validation)
    return hashlib.sha256(rfc8785.dumps(vector)).hexdigest()


def _validate_claim_header(claim: JsonObject) -> None:
    if set(claim) != CLAIM_KEYS:
        _fail("terminal publisher claim shape is open")
    claim_id = validate_claim_id(claim.get("claim_id"))
    if claim.get("kind") != "filesystem" or claim.get("purpose") != (
        "final-terminal-publisher"
    ):
        _fail("bound publisher claim has the wrong kind or purpose")
    if claim.get("status") not in {"reserved", "active"}:
        _fail("bound publisher is not reserved or active")
    if claim.get("dependency_claim_ids") != []:
        _fail("terminal publisher cannot depend on a task claim")
    if claim.get("runner_creation") is not None:
        _fail("terminal publisher cannot carry runner state")
    if claim.get("candidate_envelope_binding") is not None:
        _fail("terminal publisher cannot carry a candidate binding")
    reserved = claim.get("reserved_at_utc")
    activated = claim.get("activated_at_utc")
    verified = claim.get("last_verified_at_utc")
    active = claim.get("status") == "active"
    if (
        claim.get("prepared_at_utc") is not None
        or claim.get("root_relative_path") != f"claims/{claim_id}"
        or not _timestamp(reserved)
        or not _timestamp(verified)
        or (active and not _timestamp(activated))
        or (not active and activated is not None)
        or not isinstance(reserved, str)
        or not isinstance(verified, str)
        or reserved > verified
        or (isinstance(activated, str) and not reserved <= activated <= verified)
    ):
        _fail("terminal publisher state or timestamp matrix is invalid")


def _validate_authorizations(items: list[JsonObject], root: Path) -> None:
    if len(items) != len(AUTHORIZATIONS):
        _fail("terminal publisher authorization set is incomplete")
    for item, (identifier, predecessors, paths) in zip(
        items, AUTHORIZATIONS, strict=True
    ):
        if set(item) != AUTHORIZATION_KEYS:
            _fail("terminal publisher authorization shape is open")
        actual_paths = _strings(item.get("relative_paths"), "relative paths")
        if paths is None:
            _validate_f3_paths(actual_paths)
        elif actual_paths != list(paths):
            _fail("terminal publisher authorization path differs from contract")
        if (
            item.get("authorization_id") != identifier
            or item.get("predecessor_authorization_ids") != list(predecessors)
            or item.get("output_kind") != "final-terminal"
            or item.get("governing_lock") != "final"
            or item.get("root_path") != str(root)
            or item.get("mode") != MODE_IMMUTABLE
            or item.get("uid") != os.geteuid()
            or item.get("gid") != os.getegid()
        ):
            _fail("terminal publisher authorization contract drifted")


def _authorization_vector(
    validation: _AuthorizationValidation,
) -> list[JsonObject]:
    status = validation.claim.get("status")
    observations = _objects(
        validation.observed.get("published_outputs"), "observations"
    )
    if status == "reserved":
        if observations:
            _fail("reserved publisher invented authorization observations")
        observations = [_unpublished(item) for item in validation.authorizations]
    elif len(observations) != len(validation.authorizations):
        _fail("active publisher authorization observations are incomplete")
    published: set[str] = set()
    for authorization, observation in zip(
        validation.authorizations, observations, strict=True
    ):
        validate_output_observation(
            authorization, observation, validation.root, published
        )
        if observation.get("status") == "published":
            published.add(str(observation["authorization_id"]))
    return observations


def _unpublished(item: JsonObject) -> JsonObject:
    return {
        "authorization_id": item["authorization_id"],
        "entries": [],
        "governing_lock": item["governing_lock"],
        "output_kind": item["output_kind"],
        "root_path": item["root_path"],
        "status": "unpublished",
    }


def _validate_f3_paths(paths: list[str]) -> None:
    if not paths or paths != sorted(set(paths)):
        _fail("F3 runtime artifact paths are not sorted and unique")
    if any(
        not str(canonical_relative_path(path)).startswith("F3/")
        or PurePosixPath(path).suffix not in {".json", ".png"}
        for path in paths
    ):
        _fail("F3 runtime artifact authorization escaped its closed media set")


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


def _timestamp(value: JsonValue) -> bool:
    return isinstance(value, str) and TIMESTAMP.fullmatch(value) is not None


def _fail(message: str) -> Never:
    raise IsolationError(message)


publisher_input_state = _publisher_input_state
entry_present = _entry_present
