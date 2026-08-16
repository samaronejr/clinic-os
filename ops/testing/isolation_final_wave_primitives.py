"""Primitive boundary validation for final-wave controller records."""

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
    r"^/[^\x00\r\n]*/final-wave-controller\.lease$", re.ASCII
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
FORMS: Final = {"inputs", "scope-pre", "pre-f4", "final"}
STATES: Final = {
    "prepared",
    "child-barrier",
    "child-running",
    "child-terminal",
    "between-stages",
    "recovering",
    "failure-ready",
    "success",
}
STAGES: Final = {None, "f4-decision", "final-freeze"}
STAGING_STATES: Final = {None, "unreserved", "reserved", "active", "released"}
LEASE_STATES: Final = {"held", "released"}
TERMINATION_KINDS: Final = {None, "wait-result", "boot-disappearance"}
RELINQUISH_KINDS: Final = {
    None,
    "clean-release",
    "stale-owner-recovery",
    "changed-boot-recovery",
}
NULLABLE_DIGESTS: Final = (
    "boot_disappearance_sha256",
    "typed_outcome_sha256",
)
IDLE_NULL_FIELDS: Final = (
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
        kernel.fail("final-wave lease identity is open or incomplete")
    if (
        not _is_integer(value["device"], 0)
        or not _is_integer(value["inode"], 1)
        or not _is_integer(value["mode"], 0)
        or value["mode"] != MODE_PRIVATE
        or not _is_integer(value["uid"], 0)
        or not _is_integer(value["gid"], 0)
        or not _is_integer(value["link_count"], 1)
        or value["link_count"] != 1
    ):
        kernel.fail("final-wave lease identity is invalid")


def _validate_owners(value: JsonValue) -> None:
    if not isinstance(value, list) or not value:
        kernel.fail("final-wave controller owner history is malformed")
    for index, owner in enumerate(value, 1):
        if not isinstance(owner, dict) or set(owner) != OWNER_KEYS:
            kernel.fail("final-wave controller owner is open or incomplete")
        if (
            not _is_integer(owner["sequence"], 1)
            or owner["sequence"] != index
            or not kernel.is_uuid(owner["boot_id"])
            or not _is_integer(owner["pid"], 1)
            or not _is_integer(owner["start_ticks"], 1)
            or not _is_timestamp(owner["acquired_at_utc"])
            or (
                owner["relinquished_at_utc"] is not None
                and not _is_timestamp(owner["relinquished_at_utc"])
            )
            or not _is_scalar_enum(owner["relinquish_kind"], RELINQUISH_KINDS)
        ):
            kernel.fail("final-wave controller owner primitive is invalid")


def _validate_child_outcome_primitives(record: JsonObject) -> None:
    exit_code, signal = record["wait_exit_code"], record["wait_signal"]
    if exit_code is not None and not _is_integer(exit_code, 0, 255):
        kernel.fail("final-wave child exit code is invalid")
    if signal is not None and not _is_integer(signal, 1):
        kernel.fail("final-wave child signal is invalid")
    if not isinstance(record["timed_out"], bool) or not isinstance(
        record["cleanup_verified"], bool
    ):
        kernel.fail("final-wave child boolean is invalid")


def _validate_child_primitives(record: JsonObject) -> None:
    for key in ("child_pid", "child_pgid", "child_start_ticks"):
        value = record[key]
        if value is not None and not _is_integer(value, 1):
            kernel.fail("final-wave child integer is invalid")
    started = record["started_at_utc"]
    if started is not None and not _is_timestamp(started):
        kernel.fail("final-wave child timestamp is invalid")
    if not _is_scalar_enum(record["child_barrier_released"], {None, False, True}):
        kernel.fail("final-wave child barrier primitive is invalid")
    if not _is_scalar_enum(record["termination_kind"], TERMINATION_KINDS):
        kernel.fail("final-wave termination kind is invalid")
    _validate_child_outcome_primitives(record)
    state = record["state"]
    if state in {"prepared", "between-stages"} and (
        any(record[key] is not None for key in IDLE_NULL_FIELDS)
        or record["timed_out"] is not False
        or record["cleanup_verified"] is not False
    ):
        kernel.fail("final-wave idle child primitives are invalid")
    if state in {"child-barrier", "child-running"} and (
        any(
            record[key] is not None
            for key in ("wait_exit_code", "wait_signal", "typed_outcome_sha256")
        )
        or record["cleanup_verified"] is not False
    ):
        kernel.fail("final-wave active child primitives are invalid")


def _validate_final_wave_primitives(record: JsonObject) -> None:
    if not _is_integer(record["schema_version"], 1) or record["schema_version"] != 1:
        kernel.fail("final-wave schema version is invalid")
    if (
        not kernel.is_uuid(record["attempt_id"])
        or not _is_sha40(record["sha"])
        or not _is_scalar_enum(record["form"], FORMS)
        or not kernel.is_uuid(record["creation_boot_id"])
        or (
            record["recovery_boot_id"] is not None
            and not kernel.is_uuid(record["recovery_boot_id"])
        )
        or not _is_scalar_enum(record["state"], STATES)
        or not _is_scalar_enum(record["stage"], STAGES)
        or not _is_scalar_enum(record["final_gate_staging_state"], STAGING_STATES)
        or not _is_scalar_enum(record["controller_lease_state"], LEASE_STATES)
    ):
        kernel.fail("final-wave root primitive is invalid")
    completed = record["completed_stages"]
    if completed not in (
        [],
        ["f4-decision"],
        ["f4-decision", "final-freeze"],
    ):
        kernel.fail("final-wave completed stages are invalid")
    if any(
        record[key] is not None and not kernel.is_sha256(record[key])
        for key in NULLABLE_DIGESTS
    ) or not kernel.is_sha256(record["child_argv_sha256"]):
        kernel.fail("final-wave digest primitive is invalid")
    claim = record["final_gate_staging_claim_id"]
    if claim is not None and not kernel.is_uuid(claim):
        kernel.fail("final-wave staging claim primitive is invalid")
    lease_path = record["controller_lease_path"]
    if not isinstance(lease_path, str) or LEASE_PATH.fullmatch(lease_path) is None:
        kernel.fail("final-wave lease path is invalid")
    _validate_lease_identity(record["controller_lease_identity"])
    _validate_owners(record["controller_owners"])
    if not _is_timestamp(record["updated_at_utc"]):
        kernel.fail("final-wave update timestamp is invalid")
    _validate_child_primitives(record)


validate_final_wave_primitives = _validate_final_wave_primitives
is_sha40 = _is_sha40
is_timestamp = _is_timestamp
