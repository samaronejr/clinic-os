"""Validate the exact delegated-cgroup capability-probe journal."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final, Never

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue

PROBE_KEYS: Final = {
    "attempt_id",
    "child_device",
    "child_inode",
    "child_path",
    "creation_boot_id",
    "expected_parent_pid",
    "parent_device",
    "parent_inode",
    "parent_path",
    "probe_barrier_released",
    "probe_pgid",
    "probe_pid",
    "probe_start_ticks",
    "proof_sha256",
    "purpose",
    "recovery_boot_id",
    "removal_kind",
    "removed_at_utc",
    "schema_version",
    "state",
    "updated_at_utc",
}
STATES: Final = (
    "prepared",
    "mkdir-intent",
    "child-ready",
    "probe-identity",
    "probe-migrated",
    "probe-running",
    "kill-complete",
    "remove-intent",
    "removed",
)
PURPOSES: Final = frozenset({"todo1-kickoff", "final-input-freeze"})
UUID: Final = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP: Final = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)
PROCESS_KEYS: Final = (
    "expected_parent_pid",
    "probe_pid",
    "probe_pgid",
    "probe_start_ticks",
)


def validate_probe_journal(journal: JsonObject) -> None:
    """Require the closed root, scalar formats, and state nullability matrix."""
    if set(journal) != PROBE_KEYS or journal.get("schema_version") != 1:
        _fail("capability-probe journal has an open or unknown root")
    _match(journal.get("attempt_id"), UUID, "attempt ID")
    _match(journal.get("creation_boot_id"), UUID, "creation boot ID")
    _nullable_match(journal.get("recovery_boot_id"), UUID, "recovery boot ID")
    _match(journal.get("proof_sha256"), SHA256, "proof SHA-256")
    _match(journal.get("updated_at_utc"), TIMESTAMP, "update timestamp")
    if journal.get("purpose") not in PURPOSES or journal.get("state") not in STATES:
        _fail("capability-probe purpose or state is unknown")
    parent = _absolute(journal.get("parent_path"), "parent path")
    child = _absolute(journal.get("child_path"), "child path")
    expected_name = (
        f"clinic-os-phase1a-probe-{journal['attempt_id']}-{journal['purpose']}"
    )
    if child.parent != parent or child.name != expected_name:
        _fail("capability-probe child path is noncanonical")
    _integer(journal.get("parent_device"), "parent device")
    _integer(journal.get("parent_inode"), "parent inode")
    _validate_state_fields(journal, str(journal["state"]))


def _validate_state_fields(journal: JsonObject, state: str) -> None:
    child_values = (journal.get("child_device"), journal.get("child_inode"))
    process_values = tuple(journal.get(key) for key in PROCESS_KEYS)
    barrier = journal.get("probe_barrier_released")
    if state == "removed" and journal.get("removal_kind") == "boot-disappearance":
        if not _is_disappearance_prefix(child_values, process_values, barrier):
            _fail("boot-disappearance journal has an impossible durable prefix")
    else:
        _validate_identity_fields(state, child_values, process_values, barrier)
    _validate_removal_fields(journal, state)


def _validate_identity_fields(
    state: str,
    child_values: tuple[JsonValue, JsonValue],
    process_values: tuple[JsonValue, ...],
    barrier: JsonValue,
) -> None:
    if state == "remove-intent" and _is_disappearance_prefix(
        child_values,
        process_values,
        barrier,
    ):
        return
    before_child = state in {"prepared", "mkdir-intent"}
    if before_child:
        if any(value is not None for value in child_values + process_values) or (
            barrier is not None
        ):
            _fail("capability-probe fields precede their durable child state")
    else:
        for value in child_values:
            _integer(value, "child identity")
    _validate_process_fields(state, process_values, barrier)


def _is_disappearance_prefix(
    child_values: tuple[JsonValue, JsonValue],
    process_values: tuple[JsonValue, ...],
    barrier: JsonValue,
) -> bool:
    child_absent = all(value is None for value in child_values)
    child_present = all(_is_integer(value) for value in child_values)
    process_absent = all(value is None for value in process_values)
    process_present = all(_is_integer(value) for value in process_values)
    return (
        (child_absent and process_absent and barrier is None)
        or (child_present and process_absent and barrier is None)
        or (child_present and process_present and isinstance(barrier, bool))
    )


def _validate_process_fields(
    state: str,
    process_values: tuple[JsonValue, ...],
    barrier: JsonValue,
) -> None:
    if state == "child-ready" and (
        any(value is not None for value in process_values) or barrier is not None
    ):
        _fail("child-ready journal invented a process identity")
    if state in {"probe-identity", "probe-migrated"}:
        _process_identity(process_values)
        if barrier is not False:
            _fail("pre-release probe journal has the wrong barrier state")
    if state == "probe-running":
        _process_identity(process_values)
        if barrier is not True:
            _fail("released probe journal has the wrong barrier state")
    if state in {"kill-complete", "remove-intent", "removed"}:
        _process_identity(process_values)
        if not isinstance(barrier, bool):
            _fail("post-kill probe journal lacks its barrier disposition")


def _validate_removal_fields(journal: JsonObject, state: str) -> None:
    removal_kind = journal.get("removal_kind")
    removed_at = journal.get("removed_at_utc")
    if state != "removed":
        if removal_kind is not None or removed_at is not None:
            _fail("nonterminal probe journal carries removal completion")
    elif removal_kind not in {"rmdir", "boot-disappearance"}:
        _fail("removed probe journal lacks its removal kind")
    elif not isinstance(removed_at, str) or TIMESTAMP.fullmatch(removed_at) is None:
        _fail("removed probe journal lacks its removal timestamp")
    recovery_boot_id = journal.get("recovery_boot_id")
    if state != "removed" and recovery_boot_id is not None:
        _fail("active probe journal carries stale-boot recovery identity")
    if state == "removed" and (
        (removal_kind == "rmdir" and recovery_boot_id is not None)
        or (removal_kind == "boot-disappearance" and recovery_boot_id is None)
    ):
        _fail("probe removal kind disagrees with its recovery boot")


def _process_identity(values: tuple[JsonValue, ...]) -> None:
    for value in values:
        _integer(value, "probe process identity")


def _is_integer(value: JsonValue) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _absolute(value: JsonValue, context: str) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        _fail(f"{context} is not absolute")
    return Path(value)


def _integer(value: JsonValue, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(f"{context} is not a nonnegative integer")
    return value


def _nullable_match(
    value: JsonValue,
    pattern: re.Pattern[str],
    context: str,
) -> None:
    if value is not None:
        _match(value, pattern, context)


def _match(value: JsonValue, pattern: re.Pattern[str], context: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        _fail(f"{context} has an invalid format")


def _fail(message: str) -> Never:
    raise IsolationError(message)
