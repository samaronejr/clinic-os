"""Validate and durably recover the canonical F3 supervisor journal."""

from __future__ import annotations

import json
import os
from importlib import import_module
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Final,
    Never,
    Protocol,
    TypedDict,
    Unpack,
    runtime_checkable,
)

from ops.testing.isolation_common import JsonObject, canonical_bytes

if TYPE_CHECKING:
    from ops.testing.f3_kill_domain import PathIdentity

JOURNAL_KEYS: Final = (
    "schema_version",
    "attempt_id",
    "sha",
    "creation_boot_id",
    "recovery_boot_id",
    "inputs_sha256",
    "launcher_manifest_sha256",
    "state",
    "supervisor_pid",
    "supervisor_start_ticks",
    "cgroup_parent_path",
    "cgroup_parent_device",
    "cgroup_parent_inode",
    "cgroup_path",
    "cgroup_device",
    "cgroup_inode",
    "controller_pid",
    "controller_pgid",
    "controller_start_ticks",
    "controller_barrier_released",
    "active_child_kind",
    "active_child_name",
    "active_child_pid",
    "active_child_pgid",
    "active_child_start_ticks",
    "active_child_barrier_released",
    "heartbeat_sequence",
    "heartbeat_at_utc",
    "publication_records",
    "started_at_utc",
    "controller_ended_at_utc",
    "termination_kind",
    "boot_disappearance_sha256",
    "wait_exit_code",
    "wait_signal",
    "timed_out",
    "typed_outcome_sha256",
    "kill_domain_removal_kind",
    "kill_domain_removed_at_utc",
    "cleanup_verified",
    "updated_at_utc",
)
STATES: Final = (
    "prepared",
    "kill-domain-intent",
    "kill-domain-ready",
    "controller-barrier",
    "controller-running",
    "controller-terminal",
    "recovering",
    "kill-domain-remove-intent",
    "kill-domain-removed",
    "success",
    "failure-ready",
)
MUTABLE_MODE: Final = 0o600
SEALED_MODE: Final = 0o400


class F3RecoveryError(RuntimeError):
    """Reject journal schema, identity, transition, or durability drift."""


class _PreparedRecordInput(TypedDict):
    attempt_id: str
    sha: str
    boot_id: str
    inputs_sha256: str
    launcher_manifest_sha256: str
    supervisor_pid: int
    supervisor_start_ticks: int
    cgroup_parent: PathIdentity
    now: str


@runtime_checkable
class _Validator(Protocol):
    def validate(self, value: object) -> None: ...


class _ValidatorFactory(Protocol):
    def __call__(self, schema: object) -> _Validator: ...


def _fail(reason: str, *, cause: BaseException | None = None) -> Never:
    if cause is None:
        raise F3RecoveryError(reason)
    raise F3RecoveryError(reason) from cause


def prepared_record(**values: Unpack[_PreparedRecordInput]) -> JsonObject:
    """Construct the exact secret-free first journal state before any child."""
    record: JsonObject = {
        "schema_version": 1,
        "attempt_id": values["attempt_id"],
        "sha": values["sha"],
        "creation_boot_id": values["boot_id"],
        "recovery_boot_id": None,
        "inputs_sha256": values["inputs_sha256"],
        "launcher_manifest_sha256": values["launcher_manifest_sha256"],
        "state": "prepared",
        "supervisor_pid": values["supervisor_pid"],
        "supervisor_start_ticks": values["supervisor_start_ticks"],
        "cgroup_parent_path": str(values["cgroup_parent"].path),
        "cgroup_parent_device": values["cgroup_parent"].device,
        "cgroup_parent_inode": values["cgroup_parent"].inode,
        "cgroup_path": None,
        "cgroup_device": None,
        "cgroup_inode": None,
        "controller_pid": None,
        "controller_pgid": None,
        "controller_start_ticks": None,
        "controller_barrier_released": None,
        "active_child_kind": None,
        "active_child_name": None,
        "active_child_pid": None,
        "active_child_pgid": None,
        "active_child_start_ticks": None,
        "active_child_barrier_released": None,
        "heartbeat_sequence": None,
        "heartbeat_at_utc": None,
        "publication_records": [],
        "started_at_utc": values["now"],
        "controller_ended_at_utc": None,
        "termination_kind": None,
        "boot_disappearance_sha256": None,
        "wait_exit_code": None,
        "wait_signal": None,
        "timed_out": False,
        "typed_outcome_sha256": None,
        "kill_domain_removal_kind": None,
        "kill_domain_removed_at_utc": None,
        "cleanup_verified": False,
        "updated_at_utc": values["now"],
    }
    validate_record(record)
    return record


def validate_record(value: object) -> None:
    """Require the checked-in schema, exact keys, and exact state vocabulary."""
    if not isinstance(value, dict) or set(value) != set(JOURNAL_KEYS):
        _fail("F3 journal schema rejected")
    schema_path = Path(__file__).with_name("f3-supervisor-state.schema.json")
    schema: object = json.loads(schema_path.read_bytes())
    validator, validation_error = _jsonschema()
    try:
        validator(schema).validate(value)
    except (validation_error, TypeError) as error:
        _fail("F3 journal schema rejected", cause=error)


class JournalStore:
    """Remain the sole atomic mode-aware writer for one canonical journal."""

    def __init__(self, path: Path) -> None:
        """Bind the store to one absolute non-symlink journal path."""
        if not path.is_absolute() or path.is_symlink():
            _fail("F3 journal path rejected")
        self.path = path

    def create(self, record: JsonObject) -> None:
        """Create and fsync the first mode-0600 prepared record."""
        if self.path.exists() or record.get("state") != "prepared":
            _fail("F3 prepared journal rejected")
        validate_record(record)
        _write_new(self.path, canonical_bytes(record), MUTABLE_MODE)

    def replace(self, record: JsonObject) -> None:
        """Atomically replace a mutable journal under its caller-held lock."""
        if _mode(self.path) != MUTABLE_MODE or record.get("state") in {
            "success",
            "failure-ready",
        }:
            _fail("F3 mutable journal replacement rejected")
        validate_record(record)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        _write_new(temporary, canonical_bytes(record), MUTABLE_MODE)
        temporary.replace(self.path)
        _fsync(self.path.parent)

    def seal(self, record: JsonObject) -> None:
        """Write one terminal state and change mode from 0600 to 0400 once."""
        if _mode(self.path) != MUTABLE_MODE or record.get("state") not in {
            "success",
            "failure-ready",
        }:
            _fail("F3 terminal journal seal rejected")
        validate_record(record)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.seal")
        _write_new(temporary, canonical_bytes(record), MUTABLE_MODE)
        descriptor = os.open(temporary, os.O_RDONLY)
        try:
            os.fchmod(descriptor, SEALED_MODE)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        temporary.replace(self.path)
        _fsync(self.path.parent)


def _write_new(path: Path, raw: bytes, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync(path.parent)


def _mode(path: Path) -> int:
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        _fail("F3 journal identity rejected")
    return path.stat().st_mode & 0o777


def _fsync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _jsonschema() -> tuple[_ValidatorFactory, type[Exception]]:
    module = import_module("jsonschema")
    exceptions = import_module("jsonschema.exceptions")
    validator = getattr(module, "Draft202012Validator", None)
    validation_error = getattr(exceptions, "ValidationError", None)
    if not callable(validator) or not isinstance(validation_error, type):
        _fail("F3 journal validator is unavailable")

    def factory(schema: object) -> _Validator:
        candidate = validator(schema)
        if not isinstance(candidate, _Validator):
            _fail("F3 journal validator is unavailable")
        return candidate

    error_type: type[Exception] = validation_error
    return factory, error_type
