"""Validate the closed terminal-revalidation artifact authority fields."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Never

import rfc8785

from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_sha256,
    raw_sha256,
)

if TYPE_CHECKING:
    from ops.testing.isolation_terminal_records import TerminalValidationInputs

ARTIFACT_KEYS: Final = {
    "accepted_close_prepared_sha256",
    "approvals_sha256",
    "attempt_id",
    "boot_changed",
    "boot_observation",
    "boot_observation_sha256",
    "current_boot_id",
    "final_relative_path",
    "final_sha256",
    "lock_identity_sha256",
    "post_update_ledger_sha256",
    "pre_update_ledger_sha256",
    "previous_boot_id",
    "publisher_final_journal_sha256",
    "purpose",
    "reboot_stable_baseline_sha256",
    "schema_version",
    "sha",
    "shared_evidence_manifest_sha256",
    "tree_sha",
    "verified_at_utc",
}
SHA40: Final = re.compile(r"^[0-9a-f]{40}$")
SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class TerminalArtifactAuthority:
    """Closed inputs for one USER or accepted-close terminal artifact."""

    artifact: JsonObject
    validation: TerminalValidationInputs
    current_boot: str
    allow_prepared_predecessor: bool


def validate_terminal_artifact(authority: TerminalArtifactAuthority) -> None:
    """Require exact root, ledger, boot, observation, and stable bindings."""
    artifact = authority.artifact
    inputs = authority.validation
    ledger = inputs.ledger
    if set(artifact) != ARTIFACT_KEYS or artifact.get("schema_version") != 1:
        _fail("terminal revalidation has an open or unknown root")
    _validate_artifact_authority(authority)
    previous_boot = artifact.get("previous_boot_id")
    changed = artifact.get("boot_changed")
    if not isinstance(previous_boot, str) or not isinstance(changed, bool):
        _fail("terminal revalidation boot relation is invalid")
    if changed != (previous_boot != authority.current_boot):
        _fail("terminal revalidation boot-change flag is inconsistent")
    observation = _object(artifact.get("boot_observation"), "boot observation")
    if observation != ledger.get("boot_observation"):
        _fail("terminal boot observation differs from current ledger")
    if raw_sha256(rfc8785.dumps(observation)) != artifact.get(
        "boot_observation_sha256"
    ):
        _fail("terminal boot observation hash changed")
    if artifact.get("verified_at_utc") != observation.get("observed_at_utc"):
        _fail("terminal verification timestamp differs from its observation")
    _validate_stable_projection(artifact, ledger)
    _validate_hash_fields(artifact)


def _validate_artifact_authority(authority: TerminalArtifactAuthority) -> None:
    artifact = authority.artifact
    inputs = authority.validation
    ledger = inputs.ledger
    predecessor = artifact.get("accepted_close_prepared_sha256")
    if (
        artifact.get("purpose") != "terminal-final"
        or artifact.get("attempt_id") != ledger.get("attempt_id")
        or artifact.get("sha") != inputs.sha
        or artifact.get("current_boot_id") != authority.current_boot
        or ledger.get("boot_id") != authority.current_boot
        or ledger.get("claims") != []
    ):
        _fail("terminal revalidation differs from current ledger authority")
    if predecessor is not None and (
        not authority.allow_prepared_predecessor
        or not isinstance(predecessor, str)
        or SHA256.fullmatch(predecessor) is None
    ):
        _fail("terminal accepted-close predecessor is invalid")
    if raw_sha256(inputs.ledger_raw) != artifact.get("post_update_ledger_sha256"):
        _fail("current ledger differs from terminal post-update bytes")


def _validate_stable_projection(artifact: JsonObject, ledger: JsonObject) -> None:
    baseline = _object(ledger.get("baseline"), "ledger baseline")
    shared = _object(
        baseline.get("shared_evidence_manifest"),
        "shared evidence manifest",
    )
    if (
        artifact.get("reboot_stable_baseline_sha256")
        != ledger.get("reboot_stable_baseline_sha256")
        or artifact.get("shared_evidence_manifest_sha256") != shared.get("sha256")
        or artifact.get("lock_identity_sha256")
        != canonical_sha256(ledger.get("lock_identity"))
    ):
        _fail("terminal stable projection differs from the ledger")


def _validate_hash_fields(artifact: JsonObject) -> None:
    for key in (
        "approvals_sha256",
        "boot_observation_sha256",
        "final_sha256",
        "lock_identity_sha256",
        "post_update_ledger_sha256",
        "pre_update_ledger_sha256",
        "publisher_final_journal_sha256",
        "reboot_stable_baseline_sha256",
        "shared_evidence_manifest_sha256",
    ):
        _sha256(artifact.get(key), key)
    if SHA40.fullmatch(_text(artifact.get("tree_sha"), "tree SHA")) is None:
        _fail("terminal tree SHA is invalid")


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _sha256(value: JsonValue, context: str) -> str:
    text = _text(value, context)
    if SHA256.fullmatch(text) is None:
        _fail(f"{context} is not a SHA-256")
    return text


def _fail(message: str) -> Never:
    raise IsolationError(message)
