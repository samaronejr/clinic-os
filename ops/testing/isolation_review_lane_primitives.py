"""Primitive boundary validation for review-lane controller records."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final, TypeGuard

from ops.testing import isolation_controller_kernel as kernel
from ops.testing.isolation_common import MODE_PRIVATE

if TYPE_CHECKING:
    from collections.abc import Collection

    from ops.testing.isolation_common import JsonObject, JsonValue

SHA40: Final = re.compile(r"^[0-9a-f]{40}$")
TIMESTAMP: Final = re.compile(r"^\d{4}(?:-\d\d){2}T(?:\d\d:){2}\d\d\.\d{6}Z$", re.ASCII)
LEASE_PATH: Final = re.compile(
    r"^/[^\x00\r\n]*/review-lane-controllers/F[12]\.lease$", re.ASCII
)
LEASE_IDENTITY_KEYS: Final = {
    "device",
    "inode",
    "mode",
    "uid",
    "gid",
    "link_count",
}
OWNER_KEYS: Final = {
    "sequence",
    "boot_id",
    "pid",
    "start_ticks",
    "acquired_at_utc",
    "relinquished_at_utc",
    "relinquish_kind",
}
COMPLETED_KEYS: Final = {
    "stage",
    "process_claim_id",
    "child_pid",
    "child_pgid",
    "child_start_ticks",
    "barrier_released",
    "argv_sha256",
    "exit_code",
    "signal",
    "timed_out",
    "typed_outcome_sha256",
}
LANES: Final = {"F1", "F2"}
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
STAGES: Final = {"environment-sync", "prerequisites", "review"}
LEASE_STATES: Final = {"held", "released"}
FILESYSTEM_STATES: Final = {"unreserved", "reserved", "active", "released"}
TERMINATION_KINDS: Final = {None, "wait-result", "boot-disappearance"}
RELINQUISH_KINDS: Final = {
    None,
    "clean-release",
    "stale-owner-recovery",
    "changed-boot-recovery",
}
NULLABLE_UUIDS: Final = (
    "recovery_boot_id",
    "private_environment_claim_id",
    "child_process_claim_id",
)
NULLABLE_DIGESTS: Final = (
    "codex_tool_sha256",
    "uv_tool_sha256",
    "private_tree_sha256",
    "child_argv_sha256",
    "boot_disappearance_sha256",
    "typed_outcome_sha256",
    "terminal_outputs_sha256",
)
MAX_COMPLETED_STAGES: Final = 3


def _is_integer(value: JsonValue, minimum: int, maximum: int | None = None) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and value >= minimum
        and (maximum is None or value <= maximum)
    )


def _is_scalar_enum(value: JsonValue, allowed: Collection[str | bool | None]) -> bool:
    return (value is None or isinstance(value, str | bool)) and value in allowed


def _is_timestamp(value: JsonValue) -> TypeGuard[str]:
    return isinstance(value, str) and TIMESTAMP.fullmatch(value) is not None


def _is_sha40(value: JsonValue) -> TypeGuard[str]:
    return isinstance(value, str) and SHA40.fullmatch(value) is not None


def _validate_lease_identity(value: JsonValue) -> None:
    if not isinstance(value, dict) or set(value) != LEASE_IDENTITY_KEYS:
        kernel.fail("review lease identity is open or incomplete")
    if (
        not _is_integer(value["device"], 0)
        or not kernel.positive(value["inode"])
        or not _is_integer(value["mode"], 0)
        or value["mode"] != MODE_PRIVATE
        or not _is_integer(value["uid"], 0)
        or not _is_integer(value["gid"], 0)
        or value["link_count"] != 1
    ):
        kernel.fail("review lease identity primitive is invalid")


def _validate_owners(value: JsonValue) -> None:
    if not isinstance(value, list) or not value:
        kernel.fail("review controller owner history is malformed")
    for index, owner in enumerate(value, 1):
        if not isinstance(owner, dict) or set(owner) != OWNER_KEYS:
            kernel.fail("review controller owner is open or incomplete")
        released = owner["relinquished_at_utc"]
        if (
            not kernel.positive(owner["sequence"])
            or owner["sequence"] != index
            or not kernel.is_uuid(owner["boot_id"])
            or not kernel.positive(owner["pid"])
            or not kernel.positive(owner["start_ticks"])
            or not _is_timestamp(owner["acquired_at_utc"])
            or (released is not None and not _is_timestamp(released))
            or not _is_scalar_enum(owner["relinquish_kind"], RELINQUISH_KINDS)
        ):
            kernel.fail("review controller owner primitive is invalid")


def _validate_completed_item(item: JsonObject) -> None:
    exit_code, signal = item["exit_code"], item["signal"]
    if (
        not _is_scalar_enum(item["stage"], STAGES)
        or not kernel.is_uuid(item["process_claim_id"])
        or not kernel.positive(item["child_pid"])
        or not kernel.positive(item["child_pgid"])
        or not kernel.positive(item["child_start_ticks"])
        or item["barrier_released"] is not True
        or not kernel.is_sha256(item["argv_sha256"])
        or (exit_code is not None and not _is_integer(exit_code, 0, 255))
        or (signal is not None and not kernel.positive(signal))
        or not isinstance(item["timed_out"], bool)
        or not kernel.is_sha256(item["typed_outcome_sha256"])
    ):
        kernel.fail("review completed stage primitive is invalid")


def _validate_completed(value: JsonValue) -> None:
    if not isinstance(value, list) or len(value) > MAX_COMPLETED_STAGES:
        kernel.fail("review completed stage history is malformed")
    for item in value:
        if not isinstance(item, dict) or set(item) != COMPLETED_KEYS:
            kernel.fail("review completed stage is open or incomplete")
        _validate_completed_item(item)


def _validate_child_primitives(record: JsonObject) -> None:
    started = record["started_at_utc"]
    if started is not None and not _is_timestamp(started):
        kernel.fail("review child timestamp is invalid")
    for key in ("child_pid", "child_pgid", "child_start_ticks"):
        value = record[key]
        if value is not None and not kernel.positive(value):
            kernel.fail("review child integer primitive is invalid")
    if not _is_scalar_enum(record["child_barrier_released"], {None, False, True}):
        kernel.fail("review child barrier primitive is invalid")
    if not _is_scalar_enum(record["termination_kind"], TERMINATION_KINDS):
        kernel.fail("review termination kind primitive is invalid")
    exit_code, signal = record["wait_exit_code"], record["wait_signal"]
    if exit_code is not None and not _is_integer(exit_code, 0, 255):
        kernel.fail("review child exit code is invalid")
    if signal is not None and not kernel.positive(signal):
        kernel.fail("review child signal is invalid")
    for key in (
        "timed_out",
        "child_cleanup_verified",
        "filesystem_cleanup_verified",
    ):
        if not isinstance(record[key], bool):
            kernel.fail("review controller boolean primitive is invalid")


def _validate_review_lane_primitives(record: JsonObject) -> None:
    if record["schema_version"] != 1 or isinstance(record["schema_version"], bool):
        kernel.fail("review schema version is invalid")
    if (
        not kernel.is_uuid(record["attempt_id"])
        or not _is_scalar_enum(record["lane"], LANES)
        or not _is_sha40(record["sha"])
        or not kernel.is_sha256(record["inputs_sha256"])
        or not kernel.is_uuid(record["creation_boot_id"])
        or not _is_scalar_enum(record["state"], STATES)
        or not _is_scalar_enum(record["stage"], STAGES)
        or not _is_scalar_enum(record["controller_lease_state"], LEASE_STATES)
        or not _is_scalar_enum(record["filesystem_state"], FILESYSTEM_STATES)
    ):
        kernel.fail("review root primitive is invalid")
    if any(
        record[key] is not None and not kernel.is_uuid(record[key])
        for key in NULLABLE_UUIDS
    ) or any(
        record[key] is not None and not kernel.is_sha256(record[key])
        for key in NULLABLE_DIGESTS
    ):
        kernel.fail("review nullable identity or digest primitive is invalid")
    lease_path = record["controller_lease_path"]
    if not isinstance(lease_path, str) or LEASE_PATH.fullmatch(lease_path) is None:
        kernel.fail("review lease path is invalid")
    _validate_lease_identity(record["controller_lease_identity"])
    _validate_owners(record["controller_owners"])
    _validate_completed(record["completed_stages"])
    if not kernel.is_uuid(record["review_workspace_claim_id"]):
        kernel.fail("review workspace claim primitive is invalid")
    if not _is_timestamp(record["updated_at_utc"]):
        kernel.fail("review update timestamp is invalid")
    _validate_child_primitives(record)


validate_review_lane_primitives = _validate_review_lane_primitives
