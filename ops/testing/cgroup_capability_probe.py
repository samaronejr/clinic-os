"""Journal and execute the Phase 1A delegated-cgroup capability probe."""

from __future__ import annotations

import os
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Never

from ops.testing.cgroup_probe_child import run_probe_child
from ops.testing.cgroup_probe_records import (
    _initial_journal,
    _object,
    _text,
    _transition,
    _validate_completed,
    _validate_request,
)
from ops.testing.cgroup_probe_recovery import recover_same_boot_probe
from ops.testing.cgroup_probe_runtime import (
    _cgroup_members,
    _process_cgroup,
    _read_child_identity,
    _read_exact_byte,
    _validate_child_identity,
    _validate_parent,
    _wait_child,
    _wait_unpopulated,
    _write_all,
    _write_control,
)
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    canonical_bytes,
    ensure_private_directory,
    fsync_directory,
    load_json,
    stable_lock,
    utc_now,
    write_no_replace,
)

ARGUMENT_COUNT: Final = 8


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    """Authenticated immutable inputs for one capability-probe journal."""

    attempt_id: str
    attempt_root: Path
    proof_path: Path
    proof_sha256: str
    purpose: str


def _fail(message: str) -> Never:
    raise IsolationError(message)


def run_capability_probe(request: ProbeRequest) -> Path:
    """Prove delegated child creation, migration, kill, and durable removal."""
    proof, proof_raw = load_json(request.proof_path)
    _validate_request(request, proof, proof_raw)
    journal_root = request.attempt_root / "execution-host-probes"
    ensure_private_directory(journal_root)
    journal_path = journal_root / f"{request.purpose}.json"
    parent = Path(_text(proof["cgroup_parent_path"], "cgroup parent path"))
    relative = _text(proof["cgroup_relative_path"], "cgroup relative path")
    parent_identity = _object(proof["cgroup_parent_identity"], "parent identity")
    if journal_path.exists():
        recovered = recover_same_boot_probe(
            journal_path,
            request,
            parent,
            parent_identity,
        )
        if recovered is None:
            return _validate_completed(journal_path, request)
        child = Path(str(recovered["child_path"]))
        _run_child_barrier(journal_path, recovered, child, relative)
        return journal_path
    _validate_parent(parent, parent_identity)
    child_name = f"clinic-os-phase1a-probe-{request.attempt_id}-{request.purpose}"
    child = parent / child_name
    if child.exists() or child.is_symlink():
        _fail("capability-probe child already exists")
    journal = _initial_journal(request, parent, parent_identity, child)
    write_no_replace(journal_path, canonical_bytes(journal), mode=MODE_PRIVATE)
    _transition(journal_path, journal, "mkdir-intent")
    child.mkdir(mode=0o700)
    child_identity = child.stat(follow_symlinks=False)
    journal["child_device"] = child_identity.st_dev
    journal["child_inode"] = child_identity.st_ino
    _transition(journal_path, journal, "child-ready")
    try:
        _run_child_barrier(journal_path, journal, child, relative)
    except BaseException:
        _cleanup_failed_child(journal_path, journal, child)
        raise
    return journal_path


def _run_child_barrier(
    journal_path: Path,
    journal: JsonObject,
    child: Path,
    parent_relative: str,
) -> None:
    ready_read, ready_write = os.pipe2(os.O_CLOEXEC)
    release_read, release_write = os.pipe2(os.O_CLOEXEC)
    ack_read, ack_write = os.pipe2(os.O_CLOEXEC)
    parent_pid = os.getpid()
    expected_relative = f"{parent_relative.rstrip('/')}/{child.name}"
    pid = os.fork()
    if pid == 0:
        os.close(ready_read)
        os.close(release_write)
        os.close(ack_read)
        run_probe_child(
            ready_write,
            release_read,
            ack_write,
            parent_pid,
            expected_relative,
        )
    os.close(ready_write)
    os.close(release_read)
    os.close(ack_write)
    try:
        identity = _read_child_identity(ready_read)
        _validate_child_identity(identity, pid, parent_pid)
        journal.update(identity)
        journal["probe_barrier_released"] = False
        _transition(journal_path, journal, "probe-identity")
        _write_control(child / "cgroup.procs", f"{pid}\n".encode())
        if _process_cgroup(pid) != expected_relative:
            _fail("probe migration did not reach the journaled child")
        _transition(journal_path, journal, "probe-migrated")
        _write_all(release_write, b"1")
        os.close(release_write)
        release_write = -1
        if _read_exact_byte(ack_read) != b"1":
            _fail("probe child did not acknowledge migrated membership")
        journal["probe_barrier_released"] = True
        _transition(journal_path, journal, "probe-running")
        if _cgroup_members(child) != [pid]:
            _fail("probe child cgroup membership is not exact")
        _write_control(child / "cgroup.kill", b"1\n")
        _wait_child(pid)
        _wait_unpopulated(child)
        _transition(journal_path, journal, "kill-complete")
        _transition(journal_path, journal, "remove-intent")
        child.rmdir()
        journal["removal_kind"] = "rmdir"
        journal["removed_at_utc"] = utc_now()
        _transition(journal_path, journal, "removed")
        journal_path.chmod(MODE_IMMUTABLE)
        fsync_directory(journal_path.parent)
    finally:
        for descriptor in (ready_read, release_write, ack_read):
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)


def _cleanup_failed_child(path: Path, journal: JsonObject, child: Path) -> None:
    pid = journal.get("probe_pid")
    if child.is_dir() and isinstance(pid, int) and _cgroup_members(child) == [pid]:
        _write_control(child / "cgroup.kill", b"1\n")
        _wait_child(pid)
        _wait_unpopulated(child)
    if child.is_dir() and _cgroup_members(child) == []:
        _transition(path, journal, "remove-intent")
        child.rmdir()


def _main() -> int:
    arguments = sys.argv[1:]
    if len(arguments) != ARGUMENT_COUNT or arguments[0::2] != [
        "--ledger",
        "--proof",
        "--purpose",
        "--attempt-root",
    ]:
        sys.stderr.write("cgroup-capability-probe: invalid command grammar\n")
        return 2
    try:
        ledger_path = Path(arguments[1])
        ledger, _ = load_json(ledger_path)
        lock_path = Path(_text(ledger["lock_path"], "lock path"))
        with stable_lock(lock_path, create=False):
            path = run_capability_probe(
                ProbeRequest(
                    attempt_id=_text(ledger["attempt_id"], "attempt ID"),
                    attempt_root=Path(arguments[7]),
                    proof_path=Path(arguments[3]),
                    proof_sha256=_text(
                        _object(
                            ledger["execution_host_preflight"],
                            "execution-host proof",
                        )["sha256"],
                        "proof SHA-256",
                    ),
                    purpose=arguments[5],
                )
            )
    except (IsolationError, FileNotFoundError, PermissionError, OSError) as error:
        sys.stderr.write(f"cgroup-capability-probe: {error}\n")
        return 2
    sys.stdout.write(f"{path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
