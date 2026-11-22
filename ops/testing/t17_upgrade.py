"""Validate custody of a T17 upgrade before publishing canonical evidence."""

from __future__ import annotations

import json
import os
import signal
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Final, TypedDict
from uuid import uuid4

from ops.testing.isolation_archive_tree import move_no_replace
from ops.testing.isolation_common import MODE_PRIVATE, fsync_directory
from ops.testing.runtime_paths import runtime_directory

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from typing import BinaryIO

PROTECTED_DATABASE_PORT: Final = 5432


class UpgradeResources(TypedDict):
    """Resource floor used by the host preflight."""

    minimum_mem_available_kib: int


class UpgradePlan(TypedDict):
    """Required execution fields; the host may add provenance to the receipt."""

    head: str
    tree: str
    output: str
    argv: list[str]
    resources: UpgradeResources
    timeout_seconds: int


class DatabaseIdentity(TypedDict):
    """Fields used to check the owned lease's endpoint and teardown."""

    id: str
    host_port: int


@dataclass(frozen=True, slots=True)
class UpgradeHost:
    """Host adapters supplied by the locked owner-side driver."""

    assert_idle: Callable[[int], dict[str, int]]
    postgres: Callable[
        [Path, str],
        AbstractContextManager[tuple[dict[str, str], DatabaseIdentity]],
    ]
    container_absent: Callable[[str], bool]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _git(worktree: Path, *args: str) -> str:
    return subprocess.check_output(  # noqa: S603 - fixed local git executable.
        ["git", "-C", str(worktree), *args],  # noqa: S607
        text=True,
    ).strip()


def _require_source(plan: UpgradePlan, worktree: Path) -> None:
    _require(
        _git(worktree, "status", "--porcelain", "--untracked-files=normal") == ""
        and _git(worktree, "rev-parse", "HEAD^{tree}") == plan["tree"]
        and _git(worktree, "rev-parse", "HEAD") == plan["head"],
        "Upgrade source changed",
    )


def _capture(
    plan: UpgradePlan,
    worktree: Path,
    environment: dict[str, str],
    log: BinaryIO,
) -> int:
    with subprocess.Popen(  # noqa: S603 - closed migration argv from the host plan.
        plan["argv"],
        cwd=worktree,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    ) as process:
        try:
            return process.wait(timeout=plan["timeout_seconds"])
        except BaseException:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise


def _rehearse(
    plan: UpgradePlan,
    worktree: Path,
    host: UpgradeHost,
    runtime_root: Path,
    log: BinaryIO,
) -> tuple[int, dict[str, bool]]:
    with runtime_directory(runtime_root, purpose="cpi-t17") as runtime:
        try:
            with host.postgres(runtime, runtime.name) as (database, identity):
                _require(
                    identity["host_port"] != PROTECTED_DATABASE_PORT,
                    "Unsafe PostgreSQL port",
                )
                environment = {
                    **{
                        key: os.environ[key]
                        for key in ("PATH", "HOME", "LANG")
                        if key in os.environ
                    },
                    **database,
                    "GITHUB_WORKSPACE": str(worktree),
                    "CLINIC_DATA_MODE": "synthetic",
                    "RUNNER_TEMP": str(runtime),
                    "TMPDIR": str(runtime),
                    "UV_NO_ENV_FILE": "1",
                }
                code = _capture(plan, worktree, environment, log)
        finally:
            base = runtime / "migration-upgrade/base"
            if base.exists():
                _git(worktree, "worktree", "remove", "--force", str(base))
    cleanup = {
        "postgres_removed": host.container_absent(identity["id"]),
        "runtime_removed": not runtime.exists(),
    }
    return code, cleanup


def _absent(path: Path) -> bool:
    return not os.path.lexists(path)


def _remove_owned(path: Path, identity: os.stat_result) -> None:
    with suppress(FileNotFoundError):
        if os.path.samestat(path.lstat(), identity):
            path.unlink()


def _publish(staged: Path, out: Path, pending: Path, receipt: Path) -> None:
    # The transcript is the commit point. A crash between renames cannot yield
    # a transcript backed by an uncommitted receipt; neither name is replaced.
    transcript_identity = staged.stat()
    receipt_identity = pending.stat()
    try:
        move_no_replace(pending, receipt)
        move_no_replace(staged, out)
    except BaseException:
        # A rename may have succeeded even if the following fsync failed.
        _remove_owned(out, transcript_identity)
        _remove_owned(receipt, receipt_identity)
        fsync_directory(out.parent)
        raise


def run(
    plan: UpgradePlan,
    worktree: Path,
    *,
    host: UpgradeHost,
    runtime_root: Path,
) -> int:
    """Under the host lock, publish only a cleaned-up, same-source PASS."""
    out = Path(plan["output"])
    receipt = out.with_suffix(".receipt.json")
    _require(_absent(out) and _absent(receipt), "T17 evidence already exists")
    _require_source(plan, worktree)
    host_receipt = host.assert_idle(plan["resources"]["minimum_mem_available_kib"])
    token = uuid4().hex
    staged = out.with_name(f".{out.name}.{token}.pending")
    pending = receipt.with_name(f".{receipt.name}.{token}.pending")
    # Stage next to the canonical paths so both renames stay on one filesystem.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(staged, flags, MODE_PRIVATE)
    try:
        with os.fdopen(descriptor, "wb") as log:
            code, cleanup = _rehearse(plan, worktree, host, runtime_root, log)
            captured = log.tell()
            log.write(f"\nexit_code={code}\n".encode())
            log.flush()
            os.fsync(log.fileno())
        _require(all(cleanup.values()), "Incomplete T17 cleanup")
        if code != 0:
            return code
        raw = staged.read_bytes()
        _require(
            b"migration-upgrade: PASS\n" in raw[:captured].splitlines(keepends=True),
            "The upgrade did not emit its PASS marker",
        )
        status = {
            **plan,
            "host_receipt": host_receipt,
            "cleanup": cleanup,
            "exit_code": code,
            "stdout_stderr_bytes": captured,
            "output_sha256": sha256(raw).hexdigest(),
        }
        descriptor = os.open(pending, flags, MODE_PRIVATE)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as record:
                json.dump(status, record, indent=2)
                record.write("\n")
                record.flush()
                os.fsync(record.fileno())
            _require_source(plan, worktree)
            _publish(staged, out, pending, receipt)
        finally:
            pending.unlink(missing_ok=True)
    finally:
        staged.unlink(missing_ok=True)
    return code
