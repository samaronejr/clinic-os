"""Crash-safe fixed-lease and controller-owner primitives."""

from __future__ import annotations

import fcntl
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Never, Protocol, TypeGuard, final

from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    fsync_directory,
    load_json,
    raw_sha256,
    regular_identity,
    stat_identity_from_fd,
    write_atomic_replace,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
UUID: Final = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab]" + r"[0-9a-f]{3}-[0-9a-f]{12}$"
)
TIMESTAMP: Final = re.compile(r"^\d{4}(?:-\d\d){2}T(?:\d\d:){2}\d\d\.\d{6}Z$", re.ASCII)
OWNER_IDENTITY_KEYS: Final = "sequence boot_id pid start_ticks acquired_at_utc"
OWNER_KEYS: Final[frozenset[str]] = frozenset(
    f"{OWNER_IDENTITY_KEYS} relinquished_at_utc relinquish_kind".split()
)
_RK: Final = re.compile(r"^(?:clean-release|(?:stale-owner|changed-boot)-recovery)$")


class _OwnerStart(Protocol):
    @property
    def boot_id(self) -> str: ...

    @property
    def owner_pid(self) -> int: ...

    @property
    def owner_start_ticks(self) -> int: ...

    @property
    def acquired_at_utc(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ChildIdentity:
    """Identity of one controller-owned child process."""

    pid: int
    pgid: int
    start_ticks: int


@dataclass(frozen=True, slots=True)
class WaitResult:
    """Typed terminal result observed for one child process."""

    exit_code: int | None
    signal: int | None
    timed_out: bool
    typed_outcome_sha256: str | None


@dataclass(frozen=True, slots=True)
class RecoveryPolicy:
    """Authority used to distinguish live and recoverable identities."""

    process_is_live: Callable[[int, int], bool]
    boot_disappearance_sha256: str | None = None


@final
class FixedLease:
    """Exclusive fixed-path controller lease."""

    __slots__ = ("descriptor", "identity", "path")

    def __init__(self, descriptor: int, path: Path, identity: JsonObject) -> None:
        """Bind the open descriptor to its immutable path identity."""
        self.descriptor: int | None = descriptor
        self.path, self.identity = path, identity

    def close(self) -> None:
        """Release the descriptor exactly once."""
        descriptor, self.descriptor = self.descriptor, None
        if descriptor is not None:
            os.close(descriptor)


def _acquire_fixed_lease(path: Path) -> FixedLease:
    if not path.is_absolute():
        _fail("controller lease path is not absolute")
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, MODE_PRIVATE)
        os.fsync(descriptor)
        fsync_directory(path.parent)
    except FileExistsError:
        descriptor = os.open(path, flags)
    identity = regular_identity(path, mode=MODE_PRIVATE)
    if os.fstat(descriptor).st_size or stat_identity_from_fd(descriptor) != identity:
        os.close(descriptor)
        _fail("controller lease identity is invalid")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        _fail("controller lease is held")
    if (
        stat_identity_from_fd(descriptor) != identity
        or regular_identity(path, mode=MODE_PRIVATE) != identity
    ):
        os.close(descriptor)
        _fail("controller lease changed while acquiring flock")
    return FixedLease(descriptor, path, identity)


def _controller_owners(record: JsonObject) -> list[JsonObject]:
    value = record.get("controller_owners")
    if not _is_json_objects(value) or not value:
        _fail("controller owner history is malformed")
    owners = value
    for index, owner in enumerate(owners, 1):
        release = owner.get("relinquish_kind")
        relinquished = owner.get("relinquished_at_utc")
        if frozenset(owner) != OWNER_KEYS or owner.get("sequence") != index:
            _fail("controller owner history is open or gapped")
        if not _is_uuid(owner.get("boot_id")) or not all(
            _positive(owner.get(key)) for key in ("pid", "start_ticks")
        ):
            _fail("controller owner identity is invalid")
        if not _is_timestamp(owner.get("acquired_at_utc")) or (
            relinquished is not None and not _is_timestamp(relinquished)
        ):
            _fail("controller owner timestamp is invalid")
        if (relinquished is not None) != (release is not None) or (
            release is not None
            and (not isinstance(release, str) or _RK.fullmatch(release) is None)
        ):
            _fail("controller owner release matrix is invalid")
        if index < len(owners) and relinquished is None:
            _fail("controller owner predecessor remains open")
    return owners


def _new_controller_owner(request: _OwnerStart, sequence: int) -> JsonObject:
    return {
        "sequence": sequence,
        "boot_id": request.boot_id,
        "pid": request.owner_pid,
        "start_ticks": request.owner_start_ticks,
        "acquired_at_utc": request.acquired_at_utc,
        "relinquished_at_utc": None,
        "relinquish_kind": None,
    }


def _recover_controller_owner(
    record: JsonObject, request: _OwnerStart, recovery: RecoveryPolicy
) -> None:
    owners = _controller_owners(record)
    last = owners[-1]
    if last["boot_id"] == request.boot_id:
        child_pid = record.get("child_pid")
        child_ticks = record.get("child_start_ticks")
        if (
            isinstance(child_pid, int)
            and isinstance(child_ticks, int)
            and _positive(child_pid)
            and _positive(child_ticks)
            and recovery.process_is_live(child_pid, child_ticks)
        ):
            _fail("controller recovery has a live child")
        owner_pid, owner_ticks = last["pid"], last["start_ticks"]
        if not _positive(owner_pid) or not _positive(owner_ticks):
            _fail("controller owner identity is invalid")
        if recovery.process_is_live(owner_pid, owner_ticks):
            _fail("free controller lease has a live recorded owner")
        kind = "stale-owner-recovery"
    else:
        if not _is_sha256(recovery.boot_disappearance_sha256):
            _fail("changed-boot recovery lacks outer authority")
        kind = "changed-boot-recovery"
        record.update(
            {
                "recovery_boot_id": request.boot_id,
                "termination_kind": "boot-disappearance",
                "boot_disappearance_sha256": recovery.boot_disappearance_sha256,
                "wait_exit_code": None,
                "wait_signal": None,
                "timed_out": False,
                "typed_outcome_sha256": None,
            }
        )
    last.update(
        {"relinquished_at_utc": request.acquired_at_utc, "relinquish_kind": kind}
    )
    owners.append(_new_controller_owner(request, len(owners) + 1))
    record.update(
        {
            "state": "recovering",
            "controller_lease_state": "held",
            "updated_at_utc": request.acquired_at_utc,
        }
    )


def _close_controller_owner(record: JsonObject, now: str) -> None:
    _controller_owners(record)[-1].update(
        {"relinquished_at_utc": now, "relinquish_kind": "clean-release"}
    )


def _validate_wait(result: WaitResult, context: str = "child") -> None:
    if (result.exit_code is None) == (result.signal is None):
        _fail(f"{context} wait result must contain exactly one outcome")
    if result.exit_code is not None and result.exit_code not in range(256):
        _fail(f"{context} exit code is invalid")
    if result.signal is not None and result.signal < 1:
        _fail(f"{context} signal is invalid")
    if result.typed_outcome_sha256 is not None and not _is_sha256(
        result.typed_outcome_sha256
    ):
        _fail(f"{context} typed outcome hash is invalid")


def _successful_wait(record: JsonObject) -> bool:
    return (
        record.get("wait_exit_code") == 0
        and record.get("wait_signal") is None
        and record.get("timed_out") is False
        and _is_sha256(record.get("typed_outcome_sha256"))
    )


def _seal_controller_record(path: Path, record: JsonObject) -> None:
    write_atomic_replace(path, canonical_bytes(record))
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        os.fchmod(descriptor, MODE_IMMUTABLE)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory(path.parent)


def _require_failure_receipt(journal_path: Path, receipt_path: Path) -> str:
    try:
        _ = regular_identity(journal_path, mode=MODE_IMMUTABLE)
        journal, journal_raw = load_json(journal_path)
        _ = regular_identity(receipt_path, mode=MODE_IMMUTABLE)
        receipt, receipt_raw = load_json(receipt_path)
    except FileNotFoundError:
        _fail("failure receipt is absent")
    if journal.get("state") != "failure-ready" or receipt.get(
        "journal_sha256"
    ) != raw_sha256(journal_raw):
        _fail("failure receipt does not bind a sealed controller")
    return raw_sha256(receipt_raw)


def _is_sha256(value: JsonValue) -> TypeGuard[str]:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _is_uuid(value: JsonValue) -> TypeGuard[str]:
    return isinstance(value, str) and UUID.fullmatch(value) is not None


def _is_timestamp(value: JsonValue) -> TypeGuard[str]:
    return isinstance(value, str) and TIMESTAMP.fullmatch(value) is not None


def _is_json_objects(value: JsonValue) -> TypeGuard[list[JsonObject]]:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def _positive(value: JsonValue) -> TypeGuard[int]:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _fail(message: str) -> Never:
    raise IsolationError(message)


acquire_fixed_lease, controller_owners = _acquire_fixed_lease, _controller_owners
new_controller_owner = _new_controller_owner
recover_controller_owner = _recover_controller_owner
close_controller_owner, validate_wait = _close_controller_owner, _validate_wait
successful_wait, seal_controller_record = _successful_wait, _seal_controller_record
require_failure_receipt, fail = _require_failure_receipt, _fail
is_sha256, is_uuid, is_json_objects = _is_sha256, _is_uuid, _is_json_objects
positive = _positive
