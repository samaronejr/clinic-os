"""Closed review-lane journal schema and record projection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Never

from ops.testing import isolation_review_lane_primitives as primitives
from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue
from ops.testing.isolation_controller_kernel import (
    controller_owners,
    is_json_objects,
    is_sha256,
    is_uuid,
    new_controller_owner,
)

if TYPE_CHECKING:
    from pathlib import Path

type ReviewLane = Literal["F1", "F2"]
SHA40: Final = re.compile(r"^[0-9a-f]{40}$")
RECORD_KEYS: Final = frozenset(
    key
    for group in (
        "schema_version attempt_id lane sha inputs_sha256 creation_boot_id",
        "recovery_boot_id state stage controller_lease_path",
        "controller_lease_identity controller_lease_state controller_owners",
        "completed_stages review_workspace_claim_id private_environment_claim_id",
        "filesystem_state codex_tool_sha256 uv_tool_sha256 private_tree_sha256",
        "child_process_claim_id child_argv_sha256 started_at_utc child_pid",
        "child_pgid child_start_ticks child_barrier_released termination_kind",
        "boot_disappearance_sha256 wait_exit_code wait_signal timed_out",
        "typed_outcome_sha256 child_cleanup_verified filesystem_cleanup_verified",
        "terminal_outputs_sha256 updated_at_utc",
    )
    for key in group.split()
)
CHILD_KEYS: Final = frozenset(
    key
    for group in (
        "stage process_claim_id child_pid child_pgid child_start_ticks",
        "barrier_released argv_sha256 exit_code signal timed_out",
        "typed_outcome_sha256",
    )
    for key in group.split()
)
STATES: Final = {
    "prepared",
    "filesystem-reserved",
    "filesystem-active",
    "child-barrier",
    "child-running",
    "child-terminal",
    "between-stages",
    "recovering",
    "failure-ready",
    "success",
}
CURRENT_CHILD_KEYS: Final = (
    "child_process_claim_id",
    "child_argv_sha256",
    "started_at_utc",
    "child_pid",
    "child_pgid",
    "child_start_ticks",
    "child_barrier_released",
    "termination_kind",
    "boot_disappearance_sha256",
    "wait_exit_code",
    "wait_signal",
    "typed_outcome_sha256",
)


@dataclass(frozen=True, slots=True)
class _ReviewLaneStart:
    attempt_id: str
    lane: ReviewLane
    sha: str
    inputs_sha256: str
    boot_id: str
    journal_path: Path
    lease_path: Path
    owner_pid: int
    owner_start_ticks: int
    acquired_at_utc: str
    review_workspace_claim_id: str
    private_environment_claim_id: str | None = None


def _validate_review_lane_start(request: _ReviewLaneStart) -> None:
    identities = (
        request.attempt_id,
        request.boot_id,
        request.review_workspace_claim_id,
    )
    if not all(is_uuid(value) for value in identities):
        _fail("review controller UUID input is invalid")
    if SHA40.fullmatch(request.sha) is None or not is_sha256(request.inputs_sha256):
        _fail("review controller digest input is invalid")
    if (request.lane == "F2") != is_uuid(request.private_environment_claim_id):
        _fail("review private environment claim is invalid")
    if not request.journal_path.is_absolute() or not request.lease_path.is_absolute():
        _fail("review controller paths are not absolute")


def _initial_review_lane_record(
    request: _ReviewLaneStart, identity: JsonObject
) -> JsonObject:
    record: JsonObject = dict.fromkeys(RECORD_KEYS)
    record.update(
        {
            "schema_version": 1,
            "attempt_id": request.attempt_id,
            "lane": request.lane,
            "sha": request.sha,
            "inputs_sha256": request.inputs_sha256,
            "creation_boot_id": request.boot_id,
            "state": "prepared",
            "stage": _review_stages(request.lane)[0],
            "controller_lease_path": str(request.lease_path),
            "controller_lease_identity": identity,
            "controller_lease_state": "held",
            "controller_owners": [new_controller_owner(request, 1)],
            "completed_stages": [],
            "review_workspace_claim_id": request.review_workspace_claim_id,
            "private_environment_claim_id": request.private_environment_claim_id,
            "filesystem_state": "unreserved",
            "timed_out": False,
            "child_cleanup_verified": False,
            "filesystem_cleanup_verified": False,
            "updated_at_utc": request.acquired_at_utc,
        }
    )
    return record


def _validate_review_lane_record(record: JsonObject) -> None:
    lane, state = record.get("lane"), record.get("state")
    if frozenset(record) != RECORD_KEYS or record.get("schema_version") != 1:
        _fail("review controller record has an open root")
    primitives.validate_review_lane_primitives(record)
    if lane not in {"F1", "F2"} or state not in STATES:
        _fail("review controller lane or state is invalid")
    _validate_identity(record, lane, state)
    stages, completed = _review_stages(lane), _review_completed(record)
    if len(completed) > len(stages) or any(
        frozenset(item) != CHILD_KEYS or item.get("stage") != stages[index]
        for index, item in enumerate(completed)
    ):
        _fail("review completed stage history is invalid")
    if record.get("stage") not in stages:
        _fail("review current stage is invalid")
    if state == "success":
        _require_successful_review_history(record)
    _validate_child(record, state)


def _validate_identity(record: JsonObject, lane: JsonValue, state: JsonValue) -> None:
    source_sha = record.get("sha")
    if not isinstance(source_sha, str) or SHA40.fullmatch(source_sha) is None:
        _fail("review controller source SHA is invalid")
    for key in ("attempt_id", "creation_boot_id", "review_workspace_claim_id"):
        if not is_uuid(record.get(key)):
            _fail("review controller UUID field is invalid")
    if not is_sha256(record.get("inputs_sha256")):
        _fail("review input digest is invalid")
    terminal = state in {"failure-ready", "success"}
    if (record.get("controller_lease_state") == "released") != terminal:
        _fail("review lease state differs from controller state")
    owners = controller_owners(record)
    if (owners[-1].get("relinquished_at_utc") is not None) != terminal:
        _fail("review owner state differs from controller state")
    private = record.get("private_environment_claim_id")
    if (lane == "F1" and private is not None) or (
        lane == "F2" and not is_uuid(private)
    ):
        _fail("review private environment claim matrix is invalid")


def _validate_child(record: JsonObject, state: JsonValue) -> None:
    identity = tuple(
        record.get(key) for key in ("child_pid", "child_pgid", "child_start_ticks")
    )
    active = state in {"child-barrier", "child-running", "child-terminal"}
    if active and not all(_positive(value) for value in identity):
        _fail("review child identity is incomplete")
    barrier = record.get("child_barrier_released")
    if (state == "child-barrier" and barrier is not False) or (
        state in {"child-running", "child-terminal"} and barrier is not True
    ):
        _fail("review child barrier matrix is invalid")
    kind = record.get("termination_kind")
    if state == "child-terminal" and kind != "wait-result":
        _fail("review terminal child lacks a wait result")
    if kind == "wait-result" and (
        (record.get("wait_exit_code") is None) == (record.get("wait_signal") is None)
        or record.get("boot_disappearance_sha256") is not None
    ):
        _fail("review wait result is invalid")
    if kind == "boot-disappearance" and (
        not is_uuid(record.get("recovery_boot_id"))
        or not is_sha256(record.get("boot_disappearance_sha256"))
        or any(
            record.get(key) is not None
            for key in ("wait_exit_code", "wait_signal", "typed_outcome_sha256")
        )
        or record.get("timed_out") is not False
    ):
        _fail("review boot-disappearance matrix is invalid")


def _review_completed_child(record: JsonObject) -> JsonObject:
    return {
        "stage": record["stage"],
        "process_claim_id": record["child_process_claim_id"],
        "child_pid": record["child_pid"],
        "child_pgid": record["child_pgid"],
        "child_start_ticks": record["child_start_ticks"],
        "barrier_released": True,
        "argv_sha256": record["child_argv_sha256"],
        "exit_code": record["wait_exit_code"],
        "signal": record["wait_signal"],
        "timed_out": record["timed_out"],
        "typed_outcome_sha256": record["typed_outcome_sha256"],
    }


def _clear_review_child(record: JsonObject) -> None:
    for key in CURRENT_CHILD_KEYS:
        record[key] = None
    record["timed_out"] = False


def _review_completed(record: JsonObject) -> list[JsonObject]:
    value = record.get("completed_stages")
    if not is_json_objects(value):
        _fail("review child history is malformed")
    return value


def _require_successful_review_history(record: JsonObject) -> None:
    completed = _review_completed(record)
    if len(completed) != len(_review_stages(record.get("lane"))) or any(
        item.get("exit_code") != 0
        or item.get("signal") is not None
        or item.get("timed_out") is not False
        or not is_sha256(item.get("typed_outcome_sha256"))
        for item in completed
    ):
        _fail("review success has a failed or incomplete completed child")


def _review_stages(lane: JsonValue) -> tuple[str, ...]:
    if lane == "F1":
        return ("review",)
    if lane == "F2":
        return ("environment-sync", "prerequisites", "review")
    return _fail("review lane is invalid")


def _positive(value: JsonValue) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _fail(message: str) -> Never:
    raise IsolationError(message)


ReviewLaneStart = _ReviewLaneStart
validate_review_lane_start = _validate_review_lane_start
initial_review_lane_record = _initial_review_lane_record
validate_review_lane_record = _validate_review_lane_record
review_completed_child = _review_completed_child
clear_review_child = _clear_review_child
review_completed = _review_completed
require_successful_review_history = _require_successful_review_history
review_stages = _review_stages
