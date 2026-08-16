"""Close prior-boot capability-probe journals without touching the new tree."""

from __future__ import annotations

import importlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Never, Protocol, cast

from ops.testing.cgroup_probe_contract import UUID, validate_probe_journal
from ops.testing.cgroup_probe_records import _transition, _validate_completed
from ops.testing.isolation_common import (
    MODE_DIRECTORY,
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    JsonValue,
    directory_identity,
    fsync_directory,
    load_json,
    regular_identity,
    utc_now,
)

PROBE_ROOT_NAME: Final = "execution-host-probes"
PROBE_PURPOSES: Final = ("todo1-kickoff", "final-input-freeze")
type ProcessMatcher = Callable[[int, int], bool]


class _ProbeRequest(Protocol):
    @property
    def attempt_id(self) -> str: ...

    @property
    def attempt_root(self) -> Path: ...

    @property
    def proof_sha256(self) -> str: ...

    @property
    def purpose(self) -> str: ...


@dataclass(frozen=True, slots=True)
class _LedgerProbeRequest:
    attempt_id: str
    attempt_root: Path
    proof_sha256: str
    purpose: str


def recover_stale_boot_probes(ledger: JsonObject, current_boot: str) -> None:
    """Close the exact prior-attempt probe set before outer stale cleanup."""
    if UUID.fullmatch(current_boot) is None:
        _fail("current boot ID has an invalid format")
    attempt_id = _text(ledger.get("attempt_id"), "attempt ID")
    attempt_root = _absolute(ledger.get("attempt_root"), "attempt root")
    if attempt_root.name != attempt_id:
        _fail("attempt root does not end in the bound attempt ID")
    proof = _object(ledger.get("execution_host_preflight"), "execution-host proof")
    proof_sha256 = _text(proof.get("sha256"), "execution-host proof SHA-256")
    parent_path = _text(proof.get("cgroup_parent_path"), "cgroup parent path")
    parent_device = _integer(proof.get("cgroup_parent_device"), "parent device")
    parent_inode = _integer(proof.get("cgroup_parent_inode"), "parent inode")
    _absolute(proof.get("path"), "execution-host proof path")

    root = attempt_root / PROBE_ROOT_NAME
    names = _probe_names(root)
    rebound = ledger.get("boot_id") == current_boot
    for purpose in PROBE_PURPOSES:
        name = f"{purpose}.json"
        if name not in names:
            if purpose == "todo1-kickoff":
                _fail("todo1 kickoff capability-probe journal is missing")
            continue
        path = root / name
        journal, _ = load_json(path)
        validate_probe_journal(journal)
        if rebound:
            if journal.get("state") != "removed":
                _fail("current-boot rebound retained an active prior probe")
            bound_sha256 = _text(journal.get("proof_sha256"), "probe proof SHA-256")
        else:
            _validate_parent_binding(journal, parent_path, parent_device, parent_inode)
            bound_sha256 = proof_sha256
        recover_stale_boot_probe(
            path,
            _LedgerProbeRequest(attempt_id, attempt_root, bound_sha256, purpose),
            current_boot,
        )


def recover_stale_boot_probe(
    path: Path,
    request: _ProbeRequest,
    current_boot: str,
) -> None:
    """Seal one authenticated prior-boot prefix from absence evidence only."""
    expected = request.attempt_root / PROBE_ROOT_NAME / f"{request.purpose}.json"
    if path != expected:
        _fail("capability-probe journal path drifted")
    journal, _ = load_json(path)
    validate_probe_journal(journal)
    _validate_binding(journal, request)
    mode = _journal_mode(path)
    state = str(journal["state"])
    if state == "removed":
        if mode == MODE_PRIVATE:
            _seal(path)
        _validate_completed(path, request)
        return
    if mode != MODE_PRIVATE:
        _fail("active capability-probe journal is not private")
    if journal["creation_boot_id"] == current_boot:
        _fail("same-boot capability probe requires same-boot recovery")

    child = Path(str(journal["child_path"]))
    _require_absent(child)
    _require_prior_process_absent(journal)
    if state != "remove-intent":
        _transition(path, journal, "remove-intent")
    _require_absent(child)
    journal["recovery_boot_id"] = current_boot
    journal["removal_kind"] = "boot-disappearance"
    journal["removed_at_utc"] = utc_now()
    _transition(path, journal, "removed")
    _seal(path)
    _validate_completed(path, request)


def _probe_names(root: Path) -> set[str]:
    identity = directory_identity(root)
    if identity.get("mode") != MODE_DIRECTORY:
        _fail("capability-probe journal root is not private")
    allowed = {f"{purpose}.json" for purpose in PROBE_PURPOSES}
    with os.scandir(root) as entries:
        names = {entry.name for entry in entries}
    if not names.issubset(allowed):
        _fail("capability-probe journal root has an unexpected entry")
    return names


def _validate_parent_binding(
    journal: JsonObject,
    parent_path: str,
    parent_device: int,
    parent_inode: int,
) -> None:
    if (
        journal.get("parent_path") != parent_path
        or journal.get("parent_device") != parent_device
        or journal.get("parent_inode") != parent_inode
    ):
        _fail("capability-probe parent binding drifted")


def _validate_binding(journal: JsonObject, request: _ProbeRequest) -> None:
    if (
        journal.get("attempt_id") != request.attempt_id
        or journal.get("purpose") != request.purpose
        or journal.get("proof_sha256") != request.proof_sha256
    ):
        _fail("capability-probe journal binding drifted")


def _journal_mode(path: Path) -> int:
    value = path.stat(follow_symlinks=False)
    mode = stat.S_IMODE(value.st_mode)
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.geteuid()
        or value.st_gid != os.getegid()
        or value.st_nlink != 1
        or mode not in {MODE_PRIVATE, MODE_IMMUTABLE}
    ):
        _fail("capability-probe journal identity drifted")
    return mode


def _require_absent(child: Path) -> None:
    try:
        child.lstat()
    except FileNotFoundError:
        return
    _fail("prior-boot capability-probe child still exists")


def _require_prior_process_absent(journal: JsonObject) -> None:
    pid = journal.get("probe_pid")
    start_ticks = journal.get("probe_start_ticks")
    if pid is None and start_ticks is None:
        return
    if not isinstance(pid, int) or not isinstance(start_ticks, int):
        _fail("prior-boot capability-probe process identity is incomplete")
    recovery = importlib.import_module("ops.testing.cgroup_probe_recovery")
    matcher = cast("ProcessMatcher", vars(recovery)["_matching_process"])
    if matcher(pid, start_ticks):
        _fail("prior-boot probe process identity still exists")


def _seal(path: Path) -> None:
    path.chmod(MODE_IMMUTABLE)
    fsync_directory(path.parent)
    regular_identity(path, mode=MODE_IMMUTABLE)


def _absolute(value: JsonValue, context: str) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        _fail(f"{context} is not absolute")
    return Path(value)


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _integer(value: JsonValue, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(f"{context} must be a nonnegative integer")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
