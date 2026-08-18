"""Reserve, activate, supervise, and release review-stage process claims."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Never

from ops.testing.isolation_barrier_process import (
    BarrierProcess,
    barrier_argv,
    start_barrier_process,
)
from ops.testing.isolation_claim_records import claim_objects
from ops.testing.isolation_claim_transitions import (
    activate_claim,
    release_claim,
    reserve_claim,
)
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
    raw_sha256,
    write_no_replace,
)
from ops.testing.isolation_process_observation import observe_process_member

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ops.testing.isolation_controller_kernel import WaitResult
    from ops.testing.isolation_review_runtime_inputs import ReviewRuntimeInputs


@dataclass(frozen=True, slots=True)
class ReviewStageRequest:
    """Closed process-claim identity, payload, dependencies, and environment."""

    claim_id: str
    purpose: str
    dependencies: Sequence[str]
    payload: Sequence[str]
    environment: Mapping[str, str]


@dataclass(slots=True)
class ClaimedReviewStage:
    """One active process claim and its blocked controller-owned child."""

    runtime: ReviewRuntimeInputs
    claim_id: str
    claim_root: Path
    argv_sha256: str
    child: BarrierProcess

    result: WaitResult | None = None
    released: bool = False

    def wait(self, timeout_seconds: int) -> WaitResult:
        """Wait and reap while retaining the process claim for journaling."""
        if self.result is not None:
            _fail("review stage wait result was already recorded")
        self.result = self.child.wait(timeout_seconds)
        self.child.close()
        return self.result

    def release(self) -> None:
        """Remove the claim root and release its UUID after terminal journaling."""
        if self.result is None:
            _fail("review process claim cannot release before terminal wait")
        _remove_claim_root(self.claim_root, self.runtime.attempt_root / "claims")
        release_claim(self.runtime.ledger_path, self.claim_id)
        self.released = True

    def abort(self) -> None:
        """Kill/reap and release this process claim after an outer failure."""
        if self.released:
            return
        self.child.close()
        _remove_claim_root(self.claim_root, self.runtime.attempt_root / "claims")
        release_claim(self.runtime.ledger_path, self.claim_id)
        self.released = True


def start_claimed_review_stage(
    runtime: ReviewRuntimeInputs,
    request: ReviewStageRequest,
) -> ClaimedReviewStage:
    """Reserve a process claim and return its activated blocked child."""
    actual_argv = barrier_argv(request.payload)
    spec = _process_spec(
        request.claim_id,
        request.purpose,
        request.dependencies,
        actual_argv,
        request.environment,
    )
    spec_path = runtime.controller_root / f"{request.claim_id}.process-spec.json"
    write_no_replace(spec_path, canonical_bytes(spec), mode=MODE_IMMUTABLE)
    try:
        _ = reserve_claim(runtime.ledger_path, spec_path)
    finally:
        spec_path.unlink(missing_ok=True)
    claim_root = runtime.attempt_root / "claims" / request.claim_id
    child: BarrierProcess | None = None
    activated = False
    try:
        child = start_barrier_process(
            request.payload,
            request.environment,
            claim_root / "stdout",
            claim_root / "stderr",
        )
        observation: JsonObject = {
            "listener_socket_inode": None,
            "listeners": [],
            "members": [observe_process_member(child.pid)],
        }
        observation_path = runtime.controller_root / f"{request.claim_id}.process.json"
        write_no_replace(
            observation_path,
            canonical_bytes(observation),
            mode=MODE_IMMUTABLE,
        )
        try:
            activate_claim(runtime.ledger_path, request.claim_id, observation_path)
        finally:
            observation_path.unlink(missing_ok=True)
        activated = True
        return ClaimedReviewStage(
            runtime,
            request.claim_id,
            claim_root,
            _argv_sha(actual_argv),
            child,
        )
    finally:
        if not activated:
            if child is not None:
                child.close()
            _remove_claim_root(claim_root, runtime.attempt_root / "claims")
            release_claim(runtime.ledger_path, request.claim_id)


def recover_recorded_process(
    runtime: ReviewRuntimeInputs, claim_id: str | None
) -> None:
    """Remove and release a dead process claim named by the recovery journal."""
    if claim_id is None:
        return
    ledger, _ = load_json(runtime.ledger_path)
    present = any(
        item.get("claim_id") == claim_id for item in claim_objects(ledger["claims"])
    )
    if not present:
        return
    root = runtime.attempt_root / "claims" / claim_id
    _remove_claim_root(root, runtime.attempt_root / "claims")
    release_claim(runtime.ledger_path, claim_id)


def _process_spec(
    claim_id: str,
    purpose: str,
    dependencies: Sequence[str],
    argv: Sequence[str],
    environment: Mapping[str, str],
) -> JsonObject:
    launcher = Path(sys.executable)
    target = launcher.resolve(strict=True)
    identity = launcher.lstat()
    literal: list[JsonValue] = [
        {"name": name, "value": environment[name]} for name in sorted(environment)
    ]
    argv_values: list[JsonValue] = list(argv)
    dependency_values: list[JsonValue] = []
    dependency_values.extend(sorted(dependencies))
    desired: JsonObject = {
        "argv": argv_values,
        "argv_sha256": _argv_sha(argv),
        "borrowed_file_refs": [],
        "environment_contract": {
            "absent_keys": [],
            "literal": literal,
            "secret_keys": [],
        },
        "gid": os.getegid(),
        "gunicorn_config_lstat": None,
        "gunicorn_config_path": None,
        "gunicorn_config_sha256": None,
        "host_ports": [],
        "interpreter_realpath": str(target),
        "interpreter_sha256": raw_sha256(target.read_bytes()),
        "launcher_lstat": {
            "device": identity.st_dev,
            "gid": identity.st_gid,
            "inode": identity.st_ino,
            "link_count": identity.st_nlink,
            "mode": stat.S_IMODE(identity.st_mode),
            "symlink_target": (
                str(launcher.readlink()) if launcher.is_symlink() else None
            ),
            "uid": identity.st_uid,
        },
        "launcher_path": str(launcher),
        "module": None,
        "process_model": "single",
        "uid": os.geteuid(),
        "worker_count": None,
    }
    return {
        "claim_id": claim_id,
        "dependency_claim_ids": dependency_values,
        "desired": desired,
        "kind": "process",
        "purpose": purpose,
    }


def _argv_sha(argv: Sequence[str]) -> str:
    return hashlib.sha256(b"".join(item.encode() + b"\0" for item in argv)).hexdigest()


def _remove_claim_root(root: Path, claims_root: Path) -> None:
    if root.parent != claims_root or root.is_symlink():
        _fail("process cleanup root escaped its claim namespace")
    if root.exists():
        shutil.rmtree(root)
    if root.exists() or root.is_symlink():
        _fail("process claim root remains after cleanup")


def _fail(message: str) -> Never:
    raise IsolationError(message)
