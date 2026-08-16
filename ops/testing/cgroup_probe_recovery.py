"""Recover one same-boot delegated-cgroup capability probe."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from typing import Final, Never

from ops.testing.cgroup_probe_child import process_start_ticks
from ops.testing.cgroup_probe_contract import validate_probe_journal
from ops.testing.cgroup_probe_records import _Request, _transition, _validate_completed
from ops.testing.cgroup_probe_runtime import (
    _cgroup_members,
    _validate_parent,
    _wait_unpopulated,
    _write_control,
)
from ops.testing.cgroup_probe_stale_recovery import (
    recover_stale_boot_probe,
    recover_stale_boot_probes,
)
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    fsync_directory,
    load_json,
    regular_identity,
    utc_now,
)

__all__ = ("recover_stale_boot_probe", "recover_stale_boot_probes")
WAIT_SECONDS: Final = 5.0


def recover_same_boot_probe(
    path: Path,
    request: _Request,
    parent: Path,
    parent_identity: JsonObject,
) -> JsonObject | None:
    """Resume an authenticated mutable prefix or accept one sealed terminal."""
    journal, _ = load_json(path)
    validate_probe_journal(journal)
    _validate_binding(journal, request, parent)
    mode = _journal_mode(path)
    state = str(journal["state"])
    if state == "removed":
        if mode == MODE_PRIVATE:
            _seal(path)
        _validate_completed(path, request)
        return None
    if mode != MODE_PRIVATE:
        _fail("active capability-probe journal is not private")
    if journal["creation_boot_id"] != _boot_id():
        _fail("only stale-boot reconciliation may recover this probe")
    _validate_parent(parent, parent_identity)
    child = Path(str(journal["child_path"]))
    return _recover_active(path, journal, child, state)


def _recover_active(
    path: Path,
    journal: JsonObject,
    child: Path,
    state: str,
) -> JsonObject | None:
    if state in {"prepared", "mkdir-intent"}:
        return _recover_creation(path, journal, child, state)
    if state == "remove-intent":
        _remove_after_intent(path, journal, child)
        return None
    _validate_child(child, journal)
    if state == "child-ready":
        _require_empty(child, "child-ready")
        return journal
    if state == "probe-identity":
        _wait_recorded_process_absent(journal)
        _require_empty(child, "probe-identity")
        _clear_process_identity(journal)
        _transition(path, journal, "child-ready")
        return journal
    if state in {"probe-migrated", "probe-running"}:
        _kill_recorded_member(child, journal)
        _transition(path, journal, "kill-complete")
    if journal["state"] == "kill-complete":
        _wait_unpopulated(child)
        _require_empty(child, "kill-complete")
        _transition(path, journal, "remove-intent")
    _remove_after_intent(path, journal, child)
    return None


def _validate_binding(
    journal: JsonObject,
    request: _Request,
    parent: Path,
) -> None:
    if (
        journal["attempt_id"] != request.attempt_id
        or journal["purpose"] != request.purpose
        or journal["proof_sha256"] != request.proof_sha256
        or journal["parent_path"] != str(parent)
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


def _recover_creation(
    path: Path,
    journal: JsonObject,
    child: Path,
    state: str,
) -> JsonObject:
    if state == "prepared":
        if child.exists() or child.is_symlink():
            _fail("capability-probe child preceded durable creation intent")
        _transition(path, journal, "mkdir-intent")
    if child.is_symlink():
        _fail("capability-probe child is a link")
    if not child.exists():
        child.mkdir(mode=0o700)
    child_identity = _owned_child_identity(child)
    _require_empty(child, "mkdir-intent")
    journal["child_device"] = child_identity.st_dev
    journal["child_inode"] = child_identity.st_ino
    _transition(path, journal, "child-ready")
    return journal


def _validate_child(child: Path, journal: JsonObject) -> None:
    value = _owned_child_identity(child)
    if (
        value.st_dev != journal["child_device"]
        or value.st_ino != journal["child_inode"]
    ):
        _fail("capability-probe child identity drifted")


def _owned_child_identity(child: Path) -> os.stat_result:
    value = child.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.geteuid()
        or value.st_gid != os.getegid()
    ):
        _fail("capability-probe child is not an executor-owned directory")
    with os.scandir(child) as entries:
        has_nested_entry = any(
            entry.is_dir(follow_symlinks=False) or entry.is_symlink()
            for entry in entries
        )
    if has_nested_entry:
        _fail("capability-probe child contains an extra child or link")
    return value


def _require_empty(child: Path, state: str) -> None:
    if _cgroup_members(child):
        _fail(f"capability-probe membership is illegal at {state}")


def _wait_recorded_process_absent(journal: JsonObject) -> None:
    pid = _journal_integer(journal, "probe_pid")
    start = _journal_integer(journal, "probe_start_ticks")
    deadline = time.monotonic() + WAIT_SECONDS
    while _matching_process(pid, start) and time.monotonic() < deadline:
        time.sleep(0.01)
    if _matching_process(pid, start):
        _fail("pre-migration probe process survived its parent")


def _kill_recorded_member(child: Path, journal: JsonObject) -> None:
    pid = _journal_integer(journal, "probe_pid")
    start = _journal_integer(journal, "probe_start_ticks")
    members = _cgroup_members(child)
    if members == [pid] and _matching_process(pid, start):
        _write_control(child / "cgroup.kill", b"1\n")
        _wait_recorded_process_absent(journal)
    elif members or _matching_process(pid, start):
        _fail("migrated probe membership or process identity drifted")
    _wait_unpopulated(child)
    _require_empty(child, "post-kill")


def _matching_process(pid: int, start_ticks: int) -> bool:
    try:
        return process_start_ticks(pid) == start_ticks
    except (FileNotFoundError, ProcessLookupError):
        return False


def _journal_integer(journal: JsonObject, key: str) -> int:
    value = journal[key]
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"capability-probe {key} is not an integer")
    return value


def _clear_process_identity(journal: JsonObject) -> None:
    for key in ("expected_parent_pid", "probe_pgid", "probe_pid", "probe_start_ticks"):
        journal[key] = None
    journal["probe_barrier_released"] = None


def _remove_after_intent(path: Path, journal: JsonObject, child: Path) -> None:
    if child.exists() or child.is_symlink():
        _validate_child(child, journal)
        _require_empty(child, "remove-intent")
        child.rmdir()
    journal["removal_kind"] = "rmdir"
    journal["removed_at_utc"] = utc_now()
    _transition(path, journal, "removed")
    _seal(path)


def _seal(path: Path) -> None:
    path.chmod(MODE_IMMUTABLE)
    fsync_directory(path.parent)
    regular_identity(path, mode=MODE_IMMUTABLE)


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _fail(message: str) -> Never:
    raise IsolationError(message)
