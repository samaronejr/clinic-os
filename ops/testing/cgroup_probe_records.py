"""Validate and persist the closed cgroup capability-probe journal."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Final, Never, Protocol

from ops.testing.cgroup_probe_contract import (
    PURPOSES,
    UUID,
    validate_probe_journal,
)
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
    raw_sha256,
    utc_now,
    write_atomic_replace,
)

UUID_PATTERN: Final = UUID


class _Request(Protocol):
    @property
    def attempt_id(self) -> str: ...

    @property
    def attempt_root(self) -> Path: ...

    @property
    def proof_sha256(self) -> str: ...

    @property
    def purpose(self) -> str: ...


def _initial_journal(
    request: _Request,
    parent: Path,
    parent_identity: JsonObject,
    child: Path,
) -> JsonObject:
    return {
        "attempt_id": request.attempt_id,
        "child_device": None,
        "child_inode": None,
        "child_path": str(child),
        "creation_boot_id": _boot_id(),
        "expected_parent_pid": None,
        "parent_device": _integer(parent_identity["device"], "parent device"),
        "parent_inode": _integer(parent_identity["inode"], "parent inode"),
        "parent_path": str(parent),
        "probe_barrier_released": None,
        "probe_pgid": None,
        "probe_pid": None,
        "probe_start_ticks": None,
        "proof_sha256": request.proof_sha256,
        "purpose": request.purpose,
        "recovery_boot_id": None,
        "removal_kind": None,
        "removed_at_utc": None,
        "schema_version": 1,
        "state": "prepared",
        "updated_at_utc": utc_now(),
    }


def _transition(path: Path, journal: JsonObject, state: str) -> None:
    journal["state"] = state
    journal["updated_at_utc"] = utc_now()
    write_atomic_replace(path, canonical_bytes(journal))


def _validate_request(request: _Request, proof: JsonObject, raw: bytes) -> None:
    if (
        UUID_PATTERN.fullmatch(request.attempt_id) is None
        or request.purpose not in PURPOSES
        or raw_sha256(raw) != request.proof_sha256
        or proof.get("boot_id") != _boot_id()
    ):
        _fail("capability-probe request does not match its immutable proof")
    if not request.attempt_root.is_absolute() or request.attempt_root.is_symlink():
        _fail("attempt root must be an absolute non-symlink directory")


def _validate_completed(path: Path, request: _Request) -> Path:
    value = path.stat(follow_symlinks=False)
    journal, _ = load_json(path)
    validate_probe_journal(journal)
    if (
        not stat.S_ISREG(value.st_mode)
        or stat.S_IMODE(value.st_mode) != MODE_IMMUTABLE
        or journal.get("attempt_id") != request.attempt_id
        or journal.get("purpose") != request.purpose
        or journal.get("proof_sha256") != request.proof_sha256
        or journal.get("state") != "removed"
    ):
        _fail("existing capability-probe journal is not reusable")
    return path


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _integer(value: JsonValue, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{context} must be an integer")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
