"""Reobserve process claims through procfs and the closed listener inventory."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Never, cast

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue
from ops.testing.isolation_process_observation import (
    observe_process_member,
    validate_process_observation,
)


def observe_live_process(
    claim: JsonObject,
    inventory: JsonObject,
    *,
    proc_root: Path = Path("/proc"),
) -> JsonObject | None:
    """Return complete current members/listeners, exact absence, or reject drift."""
    desired = _object(claim.get("desired"), "process desired")
    recorded = _object(claim.get("observed"), "process observation")
    members = _objects(recorded.get("members"), "recorded members")
    if members:
        observed_members = _reobserve_members(members, proc_root)
        if observed_members is None:
            return None
    else:
        observed_members = _discover_reserved_member(desired, inventory, proc_root)
        if observed_members is None:
            return None
    listeners = _process_listeners(desired, inventory)
    ports = _objects(desired.get("host_ports"), "desired ports")
    if len(listeners) != len(ports):
        _fail("live process listener observation is partial")
    socket_inodes = {item.get("socket_inode") for item in listeners}
    socket_inode: JsonValue = None
    if ports:
        if len(socket_inodes) != 1:
            _fail("live process listeners do not share one socket inode")
        socket_inode = socket_inodes.pop()
    observed: JsonObject = {
        "listener_socket_inode": socket_inode,
        "listeners": cast("JsonValue", listeners),
        "members": cast("JsonValue", observed_members),
    }
    validate_process_observation(observed, claim)
    return observed


def _reobserve_members(
    recorded: list[JsonObject],
    proc_root: Path,
) -> list[JsonObject] | None:
    presence = [
        _pid_exists(proc_root, _integer(item.get("pid"), "member PID"))
        for item in recorded
    ]
    if not any(presence):
        return None
    if not all(presence):
        _fail("live process member observation is partial")
    result = [
        observe_process_member(_integer(item.get("pid"), "member PID"), proc_root)
        for item in recorded
    ]
    result.sort(key=lambda item: _integer(item.get("pid"), "member PID"))
    return result


def _discover_reserved_member(
    desired: JsonObject,
    inventory: JsonObject,
    proc_root: Path,
) -> list[JsonObject] | None:
    listeners = _process_listeners(desired, inventory)
    if not listeners:
        return None
    pids = {_integer(item.get("pid"), "listener PID") for item in listeners}
    if len(pids) != 1 or desired.get("process_model") != "single":
        _fail("reserved process discovery is not one exact single-process owner")
    member = observe_process_member(pids.pop(), proc_root)
    return [member]


def _process_listeners(
    desired: JsonObject,
    inventory: JsonObject,
) -> list[JsonObject]:
    endpoints = {
        (item.get("transport"), item.get("host"), item.get("port"))
        for item in _objects(desired.get("host_ports"), "desired ports")
    }
    listeners = [
        item
        for item in _objects(inventory.get("listeners"), "live listeners")
        if (item.get("transport"), item.get("host"), item.get("port")) in endpoints
    ]
    if any(item.get("owner_kind") != "process" for item in listeners):
        _fail("desired process endpoint has a nonprocess owner")
    return sorted(
        listeners,
        key=lambda item: (
            str(item.get("transport")),
            str(item.get("host")),
            _integer(item.get("port"), "listener port"),
        ),
    )


def _pid_exists(proc_root: Path, pid: int) -> bool:
    try:
        value = (proc_root / str(pid)).stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(value.st_mode)


def _integer(value: JsonValue, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail(f"{context} is invalid")
    return value


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} must be an object array")
    return cast("list[JsonObject]", value)


def _fail(message: str) -> Never:
    raise IsolationError(message)
