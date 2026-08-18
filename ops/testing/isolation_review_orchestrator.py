"""Execute durable F1/F2 stages through claims, barriers, and publication."""

from __future__ import annotations

import json
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Never

from ops.testing.isolation_barrier_process import barrier_argv_sha256
from ops.testing.isolation_common import (
    MODE_DIRECTORY,
    MODE_IMMUTABLE,
    IsolationError,
    load_json,
    utc_now,
)
from ops.testing.isolation_controller_kernel import RecoveryPolicy
from ops.testing.isolation_review_commands import (
    stage_deadline,
    stage_dependencies,
    stage_environment,
    stage_payload,
)
from ops.testing.isolation_review_completion import (
    ReviewRecoveryRequest,
    publish_review_success,
    recover_review_lane,
    review_approved,
)
from ops.testing.isolation_review_failure import publish_review_failure
from ops.testing.isolation_review_filesystems import (
    ReviewFilesystems,
    cleanup_review_filesystems,
    private_tree_sha256,
    reserve_and_activate_review_filesystems,
)
from ops.testing.isolation_review_lane_controller import (
    ReviewChild,
    ReviewLaneSession,
    ReviewSeal,
    acquire_review_lane,
)
from ops.testing.isolation_review_lane_record import (
    ReviewLane,
    ReviewLaneStart,
)
from ops.testing.isolation_review_process import (
    ClaimedReviewStage,
    ReviewStageRequest,
    start_claimed_review_stage,
)
from ops.testing.isolation_review_runtime_inputs import (
    ReviewRuntimeInputs,
    load_review_runtime_inputs,
)


@dataclass(frozen=True, slots=True)
class ReviewControllerRequest:
    """Closed lane, revision, and final-input invocation."""

    lane: ReviewLane
    sha: str
    inputs_path: Path


def run_review_controller(request: ReviewControllerRequest) -> int:
    """Run or resume one durable lane and return its terminal exit status."""
    runtime = load_review_runtime_inputs(request.inputs_path, request.sha)
    _ensure_controller_root(runtime.controller_root)
    journal = runtime.controller_root / f"{request.lane}.json"
    terminal = _terminal_replay(journal, runtime, request)
    if terminal is not None:
        return terminal
    review_id, private_id = _claim_ids(journal, request.lane)
    start = ReviewLaneStart(
        runtime.attempt_id,
        request.lane,
        request.sha,
        runtime.inputs_sha256,
        _boot_id(),
        journal,
        runtime.controller_root / f"{request.lane}.lease",
        os.getpid(),
        _start_ticks(os.getpid()),
        utc_now(),
        review_id,
        private_id,
    )
    session = acquire_review_lane(start, RecoveryPolicy(_process_is_live))
    filesystems: ReviewFilesystems | None = None
    try:
        if session.record["state"] == "recovering":
            return recover_review_lane(
                session,
                runtime,
                ReviewRecoveryRequest(
                    request.lane,
                    request.sha,
                    review_id,
                    private_id,
                    journal,
                ),
            )
        if session.record["state"] != "prepared":
            _fail("review controller resumed from an unsupported state")
        session.reserve_filesystems(utc_now())
        filesystems = reserve_and_activate_review_filesystems(
            runtime,
            request.lane,
            review_id,
            private_id,
            request.inputs_path,
        )
        session.activate_filesystems(filesystems.closure, utc_now())
        success = _run_stages(session, runtime, request, filesystems)
        terminal_digest = (
            publish_review_success(runtime, request.lane, filesystems)
            if success
            else None
        )
        cleanup_review_filesystems(runtime, filesystems)
        session.seal(ReviewSeal(success, utc_now(), terminal_digest, terminal_digest))
        if not success:
            _ = publish_review_failure(
                runtime.ledger_path,
                journal,
                request.lane,
                "product-assertion",
                runtime.lineage_validation_sha256,
            )
    except (IsolationError, OSError, ValueError, KeyError, json.JSONDecodeError):
        if filesystems is not None:
            cleanup_review_filesystems(runtime, filesystems)
        session.prepare_failure(utc_now())
        session.seal(
            ReviewSeal(
                success=False,
                now=utc_now(),
                terminal_outputs_sha256=None,
                publisher_authorization_sha256=None,
            )
        )
        _ = publish_review_failure(
            runtime.ledger_path,
            journal,
            request.lane,
            "io-failure",
            runtime.lineage_validation_sha256,
        )
        return 2
    else:
        return 0 if success else 2


def _run_stages(
    session: ReviewLaneSession,
    runtime: ReviewRuntimeInputs,
    request: ReviewControllerRequest,
    filesystems: ReviewFilesystems,
) -> bool:
    while True:
        stage = str(session.record["stage"])
        claim_id = str(uuid.uuid4())
        process: ClaimedReviewStage | None = None
        try:
            payload = stage_payload(
                runtime, request.lane, request.sha, filesystems, stage
            )
            session.reserve_child_claim(
                claim_id, barrier_argv_sha256(payload), utc_now()
            )
            process = start_claimed_review_stage(
                runtime,
                ReviewStageRequest(
                    claim_id,
                    f"{request.lane.casefold()}-{stage}-process",
                    stage_dependencies(filesystems),
                    payload,
                    stage_environment(runtime, request.lane, filesystems, stage),
                ),
            )
            session.record_child(
                ReviewChild(claim_id, process.argv_sha256, process.child.identity),
                utc_now(),
            )
            process.child.release()
            session.release_child(utc_now())
            result = process.wait(stage_deadline(stage))
            session.record_wait(result, utc_now())
            process.release()
        except Exception:
            if process is not None:
                process.abort()
            raise
        tree = None
        if stage == "environment-sync" and filesystems.private_root is not None:
            tree = private_tree_sha256(filesystems.private_root)
        session.advance_stage(utc_now(), tree)
        if session.record["state"] == "recovering":
            return result.exit_code == 0 and review_approved(
                request.lane, request.sha, filesystems
            )


def _claim_ids(journal: Path, lane: str) -> tuple[str, str | None]:
    if journal.exists():
        record, _ = load_json(journal)
        return str(record["review_workspace_claim_id"]), (
            str(record["private_environment_claim_id"]) if lane == "F2" else None
        )
    return str(uuid.uuid4()), str(uuid.uuid4()) if lane == "F2" else None


def _terminal_replay(
    journal: Path, runtime: ReviewRuntimeInputs, request: ReviewControllerRequest
) -> int | None:
    if not journal.exists() or stat.S_IMODE(journal.stat().st_mode) != MODE_IMMUTABLE:
        return None
    record, _ = load_json(journal)
    if (record.get("attempt_id"), record.get("sha"), record.get("inputs_sha256")) != (
        runtime.attempt_id,
        request.sha,
        runtime.inputs_sha256,
    ):
        _fail("sealed review journal belongs to another invocation")
    return 0 if record.get("state") == "success" else 2


def _ensure_controller_root(path: Path) -> None:
    path.mkdir(mode=MODE_DIRECTORY, exist_ok=True)
    if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != MODE_DIRECTORY:
        _fail("review controller root is not private")


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _start_ticks(pid: int) -> int:
    return int((Path("/proc") / str(pid) / "stat").read_text().split()[21])


def _process_is_live(pid: int, ticks: int) -> bool:
    try:
        return _start_ticks(pid) == ticks
    except FileNotFoundError:
        return False


def _fail(message: str) -> Never:
    raise IsolationError(message)
