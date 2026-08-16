"""Build the closed JSON records persisted by the first isolation snapshot."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Never, cast

import rfc8785

from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
    raw_sha256,
    regular_identity,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ops.testing.isolation_namespace import NamespaceBinding


PROOF_REQUIRED_KEYS: Final = frozenset(
    {
        "boot_id",
        "cgroup_parent_identity",
        "cgroup_parent_path",
        "foundation_sha",
        "workspace_realpath",
    }
)
ROOT_KEYS: Final = frozenset(
    {
        "schema_version",
        "attempt_id",
        "foundation_sha",
        "authority_binding",
        "worktree_realpath",
        "boot_id",
        "state",
        "created_at_utc",
        "last_verified_at_utc",
        "closed_at_utc",
        "lock_path",
        "lock_identity",
        "attempt_root",
        "approved_plan",
        "execution_host_preflight",
        "baseline",
        "reboot_stable_baseline_sha256",
        "boot_observation",
        "rejection_close",
        "claims",
    }
)


@dataclass(frozen=True, slots=True)
class SnapshotRootInputs:
    """Typed values required to assemble the closed ledger root."""

    attempt_id: str
    foundation_sha: str
    binding: NamespaceBinding | None
    worktree: Path
    boot_id: str
    created_at: str
    lock_path: Path
    lock_identity: JsonObject
    attempt_root: Path
    approved_plan: JsonObject
    proof: JsonObject | None
    baseline: JsonObject
    inventory: JsonObject


def _fail(message: str) -> Never:
    raise IsolationError(message)


def execution_proof_record(
    path: Path,
    workspace: Path,
    authority_root: Path,
    foundation_sha: str,
    boot_id: str,
) -> JsonObject:
    """Project one immutable preflight proof into its exact ledger fields."""
    expected_name = f"clinic-os-phase1a-execution-host-{boot_id}.json"
    if path.parent != authority_root or path.name != expected_name:
        _fail("execution-host proof path is not canonical")
    identity = regular_identity(path, mode=MODE_IMMUTABLE)
    proof, raw = load_json(path)
    if not PROOF_REQUIRED_KEYS.issubset(proof):
        _fail("execution-host proof is missing required fields")
    if (
        proof["boot_id"] != boot_id
        or proof["foundation_sha"] != foundation_sha
        or proof["workspace_realpath"] != str(workspace)
    ):
        _fail("execution-host proof binding does not match snapshot")
    parent = _object(proof["cgroup_parent_identity"], "cgroup parent identity")
    return {
        "boot_id": boot_id,
        "cgroup_parent_device": _integer(parent["device"]),
        "cgroup_parent_inode": _integer(parent["inode"]),
        "cgroup_parent_path": _text(proof["cgroup_parent_path"]),
        "device": identity["device"],
        "gid": identity["gid"],
        "inode": identity["inode"],
        "link_count": identity["link_count"],
        "mode": identity["mode"],
        "path": str(path),
        "sha256": raw_sha256(raw),
        "uid": identity["uid"],
    }


def baseline_record(
    binding: NamespaceBinding,
    inventory: JsonObject,
    shared_manifest: JsonObject,
    shared_path: Path,
) -> tuple[JsonObject, bytes]:
    """Bind shared evidence and normalized host inventory into one baseline."""
    shared_raw = canonical_bytes(shared_manifest)
    baseline: JsonObject = {
        "containers": inventory["containers"],
        "evidence_lstat": binding.evidence_lstat,
        "listeners": inventory["listeners"],
        "networks": inventory["networks"],
        "omo_lstat": binding.worktree_omo_lstat,
        "shared_evidence_manifest": {
            "entry_count": _integer(shared_manifest["entry_count"]),
            "path": str(shared_path),
            "sha256": raw_sha256(shared_raw),
        },
        "volumes": inventory["volumes"],
    }
    return baseline, shared_raw


def root_record(inputs: SnapshotRootInputs) -> JsonObject:
    """Assemble the exact schema-v2 open-ledger root object."""
    observation: JsonObject = {
        "boot_id": inputs.boot_id,
        "containers": inputs.inventory["containers"],
        "listeners": inputs.inventory["listeners"],
        "networks": inputs.inventory["networks"],
        "observed_at_utc": inputs.created_at,
        "volumes": inputs.inventory["volumes"],
    }
    return {
        "approved_plan": inputs.approved_plan,
        "attempt_id": inputs.attempt_id,
        "attempt_root": str(inputs.attempt_root),
        "authority_binding": _authority_binding(inputs.binding, inputs.worktree),
        "baseline": inputs.baseline,
        "boot_id": inputs.boot_id,
        "boot_observation": observation,
        "claims": [],
        "closed_at_utc": None,
        "created_at_utc": inputs.created_at,
        "execution_host_preflight": inputs.proof,
        "foundation_sha": inputs.foundation_sha,
        "last_verified_at_utc": inputs.created_at,
        "lock_identity": inputs.lock_identity,
        "lock_path": str(inputs.lock_path),
        "reboot_stable_baseline_sha256": reboot_stable_baseline_sha256(inputs.baseline),
        "rejection_close": None,
        "schema_version": 2,
        "state": "open",
        "worktree_realpath": str(inputs.worktree),
    }


def _authority_binding(
    binding: NamespaceBinding | None,
    worktree: Path,
) -> JsonObject | None:
    if binding is None:
        return None
    link = binding.worktree_omo_lstat
    link_record: JsonObject = {
        key: link[key]
        for key in (
            "type",
            "mode",
            "uid",
            "gid",
            "device",
            "inode",
            "link_target",
            "realpath",
        )
    }
    return {
        "authority_root_identity": binding.authority_root_identity,
        "authority_root_realpath": str(binding.authority_root),
        "authority_workspace_realpath": str(binding.authority_workspace),
        "worktree_omo_lstat": link_record,
        "worktree_omo_path": str(worktree / ".omo"),
    }


def reboot_stable_baseline_sha256(
    baseline: JsonObject,
    inventory: JsonObject | None = None,
) -> str:
    """Hash the reboot-stable ambient projection with optional live inventory."""
    projection: JsonObject = copy.deepcopy(baseline)
    if inventory is not None:
        for key in ("containers", "listeners", "networks", "volumes"):
            if key not in inventory:
                _fail("live inventory is missing a reboot-stable field")
            projection[key] = copy.deepcopy(inventory[key])
    for item in _object_array(projection["containers"]):
        for key in ("state", "health", "restart_count"):
            item.pop(key)
    for item in _object_array(projection["listeners"]):
        for key in ("socket_inode", "pid", "process_start_ticks"):
            item.pop(key)
    return raw_sha256(rfc8785.dumps(projection))


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _object_array(value: JsonValue) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail("ledger array is not an object array")
    return cast("list[JsonObject]", value)


def _integer(value: JsonValue) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("expected an integer")
    return value


def _text(value: JsonValue) -> str:
    if not isinstance(value, str):
        _fail("expected a string")
    return value
