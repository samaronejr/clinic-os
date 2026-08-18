"""Crash-safe state transitions for independent F1 and F2 review lanes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Never

from ops.testing.isolation_common import (
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
    regular_identity,
    write_atomic_replace,
    write_no_replace,
)
from ops.testing.isolation_controller_kernel import (
    ChildIdentity,
    FixedLease,
    RecoveryPolicy,
    WaitResult,
    acquire_fixed_lease,
    close_controller_owner,
    is_sha256,
    is_uuid,
    recover_controller_owner,
    seal_controller_record,
    successful_wait,
    validate_wait,
)
from ops.testing.isolation_review_lane_record import ReviewLane as _ReviewLane
from ops.testing.isolation_review_lane_record import (
    ReviewLaneStart,
    clear_review_child,
    initial_review_lane_record,
    require_successful_review_history,
    review_completed,
    review_completed_child,
    review_stages,
    validate_review_lane_record,
    validate_review_lane_start,
)

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class _ReviewChild:
    process_claim_id: str
    argv_sha256: str
    identity: ChildIdentity


@dataclass(frozen=True, slots=True)
class _ToolClosure:
    codex_sha256: str
    uv_sha256: str | None


@dataclass(frozen=True, slots=True)
class _ReviewSeal:
    success: bool
    now: str
    terminal_outputs_sha256: str | None
    publisher_authorization_sha256: str | None = None


class _ReviewLaneSession:
    __slots__ = ("_lease", "_path", "record")

    def __init__(self, lease: FixedLease, path: Path, record: JsonObject) -> None:
        self._lease, self._path, self.record = lease, path, record

    def reserve_filesystems(self, now: str) -> None:
        self._expect("prepared")
        self._update(now, state="filesystem-reserved", filesystem_state="reserved")

    def activate_filesystems(self, closure: _ToolClosure, now: str) -> None:
        self._expect("filesystem-reserved")
        if not is_sha256(closure.codex_sha256):
            _fail("Codex tool hash is invalid")
        if (self.record["lane"] == "F2") != is_sha256(closure.uv_sha256):
            _fail("review uv tool matrix is invalid")
        self._update(
            now,
            state="filesystem-active",
            filesystem_state="active",
            codex_tool_sha256=closure.codex_sha256,
            uv_tool_sha256=closure.uv_sha256,
        )

    def record_child(self, child: _ReviewChild, now: str) -> None:
        if self.record["state"] not in {"filesystem-active", "between-stages"}:
            _fail("review child cannot start from this state")
        if not is_uuid(child.process_claim_id) or not is_sha256(child.argv_sha256):
            _fail("review child authorization is invalid")
        identity = child.identity
        if min(identity.pid, identity.pgid, identity.start_ticks) < 1:
            _fail("review child identity is invalid")
        if (
            self.record["child_process_claim_id"] != child.process_claim_id
            or self.record["child_argv_sha256"] != child.argv_sha256
        ):
            _fail("review child differs from its journaled claim intent")
        self._update(
            now,
            state="child-barrier",
            child_process_claim_id=child.process_claim_id,
            child_argv_sha256=child.argv_sha256,
            started_at_utc=now,
            child_pid=identity.pid,
            child_pgid=identity.pgid,
            child_start_ticks=identity.start_ticks,
            child_barrier_released=False,
        )

    def reserve_child_claim(
        self, process_claim_id: str, argv_sha256: str, now: str
    ) -> None:
        if self.record["state"] not in {"filesystem-active", "between-stages"}:
            _fail("review child claim cannot reserve from this state")
        if not is_uuid(process_claim_id) or not is_sha256(argv_sha256):
            _fail("review child claim intent is invalid")
        clear_review_child(self.record)
        self._update(
            now,
            child_process_claim_id=process_claim_id,
            child_argv_sha256=argv_sha256,
        )

    def release_child(self, now: str) -> None:
        self._expect("child-barrier")
        self._update(now, state="child-running", child_barrier_released=True)

    def record_wait(self, result: WaitResult, now: str) -> None:
        self._expect("child-running")
        validate_wait(result, "review")
        self._update(
            now,
            state="child-terminal",
            termination_kind="wait-result",
            wait_exit_code=result.exit_code,
            wait_signal=result.signal,
            timed_out=result.timed_out,
            typed_outcome_sha256=result.typed_outcome_sha256,
        )

    def advance_stage(self, now: str, private_tree_sha256: str | None = None) -> None:
        self._expect("child-terminal")
        completed = review_completed(self.record)
        completed.append(review_completed_child(self.record))
        stages = review_stages(self.record["lane"])
        success, next_index = successful_wait(self.record), len(completed)
        if success and self.record["lane"] == "F2" and next_index == 1:
            if not is_sha256(private_tree_sha256):
                _fail("private environment tree hash is invalid")
            self.record["private_tree_sha256"] = private_tree_sha256
        if success:
            self.record["stage"] = stages[min(next_index, len(stages) - 1)]
        clear_review_child(self.record)
        state = (
            "between-stages" if success and next_index < len(stages) else "recovering"
        )
        self._update(now, state=state, child_cleanup_verified=True)

    def seal(self, request: _ReviewSeal) -> None:
        self._expect("recovering")
        if request.success:
            require_successful_review_history(self.record)
            if (
                not is_sha256(request.terminal_outputs_sha256)
                or request.publisher_authorization_sha256
                != request.terminal_outputs_sha256
            ):
                _fail("review publisher authorization is invalid")
            state = "success"
        else:
            if (
                request.terminal_outputs_sha256 is not None
                or request.publisher_authorization_sha256 is not None
            ):
                _fail("review failure cannot bind terminal outputs")
            state = "failure-ready"
        close_controller_owner(self.record, request.now)
        self.record.update(
            {
                "state": state,
                "filesystem_state": "released",
                "controller_lease_state": "released",
                "child_cleanup_verified": True,
                "filesystem_cleanup_verified": True,
                "terminal_outputs_sha256": request.terminal_outputs_sha256,
                "updated_at_utc": request.now,
            }
        )
        validate_review_lane_record(self.record)
        seal_controller_record(self._path, self.record)
        self._lease.close()

    def prepare_failure(self, now: str) -> None:
        if self.record["state"] in {"failure-ready", "success"}:
            _fail("terminal review controller cannot prepare another failure")
        clear_review_child(self.record)
        self._update(
            now,
            state="recovering",
            child_cleanup_verified=True,
        )

    def abandon(self) -> None:
        self._lease.close()

    def _expect(self, state: str) -> None:
        if self.record["state"] != state:
            _fail(f"review controller is not {state}")

    def _update(self, now: str, **changes: JsonValue) -> None:
        self.record.update(changes)
        self.record["updated_at_utc"] = now
        validate_review_lane_record(self.record)
        write_atomic_replace(self._path, canonical_bytes(self.record))


def _acquire_review_lane(
    request: ReviewLaneStart, recovery: RecoveryPolicy
) -> _ReviewLaneSession:
    validate_review_lane_start(request)
    lease = acquire_fixed_lease(request.lease_path)
    ready = False
    try:
        if request.journal_path.exists():
            regular_identity(request.journal_path, mode=MODE_PRIVATE)
            record, _ = load_json(request.journal_path)
            validate_review_lane_record(record)
            binding = (
                record.get("attempt_id"),
                record.get("lane"),
                record.get("sha"),
                record.get("inputs_sha256"),
                record.get("controller_lease_identity"),
            )
            expected = (
                request.attempt_id,
                request.lane,
                request.sha,
                request.inputs_sha256,
                lease.identity,
            )
            if binding != expected:
                _fail("review invocation or lease binding drifted")
            recover_controller_owner(record, request, recovery)
            write_atomic_replace(request.journal_path, canonical_bytes(record))
        else:
            record = initial_review_lane_record(request, lease.identity)
            write_no_replace(
                request.journal_path, canonical_bytes(record), mode=MODE_PRIVATE
            )
        ready = True
        return _ReviewLaneSession(lease, request.journal_path, record)
    finally:
        if not ready:
            lease.close()


def _fail(message: str) -> Never:
    raise IsolationError(message)


ReviewLane = _ReviewLane
ReviewChild = _ReviewChild
ToolClosure = _ToolClosure
ReviewSeal = _ReviewSeal
ReviewLaneSession = _ReviewLaneSession
acquire_review_lane = _acquire_review_lane
