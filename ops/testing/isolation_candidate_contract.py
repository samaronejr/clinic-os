"""Validate the closed candidate-publisher authorization and envelope shapes."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Never, cast

import rfc8785

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue

AUTHORIZATION_KEYS: Final = frozenset(
    {
        "authorization_id",
        "output_kind",
        "root_path",
        "governing_lock",
        "predecessor_authorization_ids",
        "relative_paths",
        "mode",
        "uid",
        "gid",
    }
)
ENVELOPE_KEYS: Final = frozenset(
    {
        "schema_version",
        "attempt_id",
        "claim_id",
        "authorization_id",
        "revision_sha",
        "tree_sha",
        "image_id",
        "image_contract",
        "published_at_utc",
    }
)
IMAGE_CONTRACT_KEYS: Final = frozenset(
    {
        "kind",
        "revision_sha",
        "tree_sha",
        "source_manifest_sha256",
        "source_entry_count",
        "available_suite_ids",
    }
)
SHA40: Final = re.compile(r"^[0-9a-f]{40}$")
SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
IMAGE_ID: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
SUITES: Final = frozenset({"availability", "patient", "runtime-https", "scheduling"})
AUTHORIZATION_COUNT: Final = 2


@dataclass(frozen=True, slots=True)
class CandidateContract:
    """Bind one candidate purpose to its immutable output identities."""

    kind: str
    envelope_name: str
    envelope_authorization_id: str
    history_authorization_id: str


def _fail(message: str) -> Never:
    raise IsolationError(message)


def contract_for_purpose(purpose: JsonValue) -> CandidateContract | None:
    """Return the exact candidate contract, or None for a noncandidate purpose."""
    if purpose == "candidate-application-publisher":
        return CandidateContract(
            "application",
            "application-envelope.json",
            "candidate-application-envelope",
            "candidate-application-publication-history",
        )
    if purpose == "candidate-browser-runner-publisher":
        return CandidateContract(
            "browser-runner",
            "browser-runner-envelope.json",
            "candidate-browser-runner-envelope",
            "candidate-browser-runner-publication-history",
        )
    return None


def candidate_desired(
    attempt_root: Path, claim_id: str, envelope: JsonObject
) -> JsonObject:
    """Project the canonical authorization pair for one candidate envelope."""
    image = _object(envelope.get("image_contract"), "candidate image contract")
    kind, revision = image.get("kind"), envelope.get("revision_sha")
    contract = contract_for_purpose(f"candidate-{kind}-publisher")
    if contract is None or not isinstance(revision, str):
        _fail("candidate envelope cannot project its authorization contract")
    return {
        "owned_files": [],
        "published_outputs": [
            _authorization(
                contract.envelope_authorization_id,
                "candidate-image",
                str(attempt_root / "candidate-images"),
                [],
                [f"{revision}/{contract.envelope_name}"],
            ),
            _authorization(
                contract.history_authorization_id,
                "publication-history",
                str(attempt_root / "publication-history"),
                [contract.envelope_authorization_id],
                [f"{claim_id}.json"],
            ),
        ],
    }


def validate_candidate_desired(
    desired: JsonObject,
    claim_id: str,
    contract: CandidateContract,
    attempt_root: Path | None,
) -> None:
    """Require the exact two stable-lock candidate output authorizations."""
    if desired.get("owned_files") != []:
        _fail("candidate publisher must activate before dynamic staging exists")
    outputs = _objects(desired.get("published_outputs"), "candidate authorizations")
    if len(outputs) != AUTHORIZATION_COUNT:
        _fail("candidate publisher requires exactly two authorizations")
    revision = _candidate_revision(outputs[0], contract)
    candidate_root = None if attempt_root is None else attempt_root / "candidate-images"
    history_root = (
        None if attempt_root is None else attempt_root / "publication-history"
    )
    expected = [
        _authorization(
            contract.envelope_authorization_id,
            "candidate-image",
            (
                outputs[0].get("root_path")
                if candidate_root is None
                else str(candidate_root)
            ),
            [],
            [f"{revision}/{contract.envelope_name}"],
        ),
        _authorization(
            contract.history_authorization_id,
            "publication-history",
            outputs[1].get("root_path") if history_root is None else str(history_root),
            [contract.envelope_authorization_id],
            [f"{claim_id}.json"],
        ),
    ]
    if outputs != expected:
        _fail("candidate publisher authorizations do not match the exact pair")
    for output in outputs:
        root = output.get("root_path")
        if not isinstance(root, str) or not Path(root).is_absolute():
            _fail("candidate authorization root is not absolute")


def validate_candidate_envelope(
    envelope: JsonObject,
    ledger: JsonObject,
    claim: JsonObject,
    contract: CandidateContract,
) -> None:
    """Require exact claim, authorization, image, revision, and suite binding."""
    if set(envelope) != ENVELOPE_KEYS or envelope.get("schema_version") != 1:
        _fail("candidate envelope has the wrong closed shape")
    if (
        envelope.get("attempt_id") != ledger.get("attempt_id")
        or envelope.get("claim_id") != claim.get("claim_id")
        or envelope.get("authorization_id") != contract.envelope_authorization_id
    ):
        _fail("candidate envelope identity does not match its claim")
    revision = envelope.get("revision_sha")
    tree = envelope.get("tree_sha")
    image_id = envelope.get("image_id")
    if (
        not isinstance(revision, str)
        or SHA40.fullmatch(revision) is None
        or not isinstance(tree, str)
        or SHA40.fullmatch(tree) is None
        or not isinstance(image_id, str)
        or IMAGE_ID.fullmatch(image_id) is None
    ):
        _fail("candidate envelope source or image identity is invalid")
    image = _object(envelope.get("image_contract"), "candidate image contract")
    if set(image) != IMAGE_CONTRACT_KEYS:
        _fail("candidate image contract has the wrong closed shape")
    suites = _strings(image.get("available_suite_ids"), "available suite IDs")
    if suites != sorted(set(suites)) or not set(suites) <= SUITES:
        _fail("candidate image suite set is not sorted, unique, and closed")
    if (
        image.get("kind") != contract.kind
        or image.get("revision_sha") != revision
        or image.get("tree_sha") != tree
        or (contract.kind == "application" and suites != [])
        or not isinstance(image.get("source_entry_count"), int)
        or isinstance(image.get("source_entry_count"), bool)
        or cast("int", image["source_entry_count"]) < 0
        or not isinstance(image.get("source_manifest_sha256"), str)
        or SHA256.fullmatch(cast("str", image["source_manifest_sha256"])) is None
    ):
        _fail("candidate image contract does not match its envelope")
    desired = _object(claim.get("desired"), "candidate desired")
    outputs = _objects(desired.get("published_outputs"), "candidate authorizations")
    relative_paths = outputs[0].get("relative_paths")
    if relative_paths != [f"{revision}/{contract.envelope_name}"]:
        _fail("candidate envelope revision does not match authorization path")


def envelope_binding(envelope: JsonObject) -> JsonObject:
    """Build the sole immutable binding using RFC 8785 bytes plus LF."""
    return {
        "envelope": envelope,
        "envelope_sha256": hashlib.sha256(rfc8785.dumps(envelope) + b"\n").hexdigest(),
        "schema_version": 1,
    }


def _authorization(
    authorization_id: str,
    output_kind: str,
    root_path: JsonValue,
    predecessors: list[JsonValue],
    relative_paths: list[JsonValue],
) -> JsonObject:
    return {
        "authorization_id": authorization_id,
        "gid": os.getegid(),
        "governing_lock": "stable",
        "mode": 0o400,
        "output_kind": output_kind,
        "predecessor_authorization_ids": predecessors,
        "relative_paths": relative_paths,
        "root_path": root_path,
        "uid": os.geteuid(),
    }


def _candidate_revision(output: JsonObject, contract: CandidateContract) -> str:
    if set(output) != AUTHORIZATION_KEYS:
        _fail("candidate authorization has the wrong closed key set")
    paths = output.get("relative_paths")
    if not isinstance(paths, list) or len(paths) != 1 or not isinstance(paths[0], str):
        _fail("candidate envelope authorization requires one relative path")
    revision, separator, name = paths[0].partition("/")
    if (
        separator != "/"
        or name != contract.envelope_name
        or SHA40.fullmatch(revision) is None
    ):
        _fail("candidate envelope authorization path is invalid")
    return revision


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
