"""Validate immutable stack observations and identity-free release state."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Final, Never

if TYPE_CHECKING:
    from pathlib import Path

from ops.testing.isolation_borrowed_resources import validate_borrowed_resources
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    JsonValue,
    load_json,
    regular_identity,
)
from ops.testing.isolation_stack_claim import reserved_stack_observed

OBSERVED_KEYS: Final = frozenset(
    {
        "services",
        "container_ids",
        "owned_volumes",
        "borrowed_volumes",
        "owned_networks",
        "borrowed_networks",
        "listeners",
    }
)
SERVICE_KEYS: Final = frozenset(
    {
        "name",
        "container_id",
        "image_id",
        "uid",
        "gid",
        "command",
        "state",
        "network_mode",
        "network_attachments",
        "extra_hosts",
        "volume_mounts",
        "published_ports",
        "environment_contract",
        "image_contract",
        "filesystem_contract",
    }
)
LISTENER_KEYS: Final = frozenset(
    {
        "transport",
        "host",
        "port",
        "socket_inode",
        "owner_kind",
        "container_id",
        "pid",
        "process_start_ticks",
        "executable_realpath",
        "argv_sha256",
    }
)
MAPPED_FIELDS: Final = (
    "name",
    "image_id",
    "uid",
    "gid",
    "command",
    "network_mode",
    "extra_hosts",
    "volume_mounts",
    "published_ports",
    "environment_contract",
    "image_contract",
    "filesystem_contract",
)
CONTAINER_ID_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")


def _fail(message: str) -> Never:
    raise IsolationError(message)


def load_stack_observation(
    path: Path,
    claim: JsonObject,
    *,
    required_state: str = "running",
) -> JsonObject:
    """Load and authenticate a closed inert observation against one stack claim."""
    if not path.is_absolute() or path.is_symlink():
        _fail("stack observation must be an absolute non-symlink file")
    _ = regular_identity(path, mode=MODE_IMMUTABLE)
    observed, _ = load_json(path)
    validate_stack_observation(observed, claim, required_state=required_state)
    return observed


def validate_stack_observation(
    observed: JsonObject,
    claim: JsonObject,
    *,
    required_state: str = "running",
) -> None:
    """Authenticate one closed stack observation already read from a live source."""
    if set(observed) != set(OBSERVED_KEYS):
        _fail("stack observation has the wrong closed key set")
    desired = _object(claim.get("desired"), "stack desired")
    validate_borrowed_resources(desired, observed)
    _validate_owned_volumes(desired, observed)
    _validate_owned_networks(desired, observed)
    desired_services = _objects(desired.get("services"), "desired services")
    observed_services = _objects(observed.get("services"), "observed services")
    if len(observed_services) != len(desired_services):
        _fail("stack service mapping is incomplete")
    mapped_container_ids = [
        _validate_service(actual, expected, required_state)
        for expected, actual in zip(desired_services, observed_services, strict=True)
    ]
    container_ids = sorted(set(mapped_container_ids))
    if len(container_ids) != len(mapped_container_ids):
        _fail("observed container IDs are not sorted and unique")
    if observed.get("container_ids") != _json_strings(container_ids):
        _fail("observed container ID set disagrees with service mapping")
    listeners = _objects(observed.get("listeners"), "stack listeners")
    _validate_listeners(listeners, observed_services)


def require_stack_release_ready(claim: JsonObject, claim_root: Path) -> None:
    """Require an absent mutable root and an empty recorded stack identity set."""
    try:
        _ = os.lstat(claim_root)
    except FileNotFoundError:
        pass
    else:
        _fail("claim root still exists")
    if claim.get("observed") != reserved_stack_observed():
        _fail("live stack identities remain recorded")


def _validate_owned_volumes(desired: JsonObject, observed: JsonObject) -> None:
    desired_volumes = _objects(desired.get("owned_volumes"), "owned volumes")
    observed_volumes = _objects(observed.get("owned_volumes"), "owned volumes")
    if len(desired_volumes) != len(observed_volumes):
        _fail("owned volume observation is incomplete")
    for expected, actual in zip(desired_volumes, observed_volumes, strict=True):
        for field in ("volume_name", "driver", "labels"):
            if actual.get(field) != expected.get(field):
                _fail(f"owned volume {field} drifted")


def _validate_owned_networks(desired: JsonObject, observed: JsonObject) -> None:
    desired_networks = _objects(desired.get("owned_networks"), "owned networks")
    observed_networks = _objects(observed.get("owned_networks"), "owned networks")
    if len(desired_networks) != len(observed_networks):
        _fail("owned network observation is incomplete")
    for expected, actual in zip(desired_networks, observed_networks, strict=True):
        for field in ("network_name", "driver", "internal", "attachable", "labels"):
            if actual.get(field) != expected.get(field):
                _fail(f"owned network {field} drifted")
        network_id = actual.get("network_id")
        if (
            not isinstance(network_id, str)
            or CONTAINER_ID_PATTERN.fullmatch(network_id) is None
        ):
            _fail("owned network ID is invalid")


def _validate_service(
    actual: JsonObject,
    expected: JsonObject,
    required_state: str,
) -> str:
    if set(actual) != set(SERVICE_KEYS):
        _fail("observed stack service has the wrong closed key set")
    for field in MAPPED_FIELDS:
        if actual.get(field) != expected.get(field):
            _fail(f"stack service mapping drifted at {field}")
    if actual.get("state") != required_state:
        _fail("stack service state disagrees with the transition")
    expected_refs = _objects(expected.get("network_refs"), "network references")
    attachments = _objects(actual.get("network_attachments"), "network attachments")
    projected = [
        {"aliases": item.get("aliases"), "network_name": item.get("network_name")}
        for item in attachments
    ]
    if projected != expected_refs:
        _fail("stack network attachment mapping drifted")
    for item in attachments:
        network_id = item.get("network_id")
        if (
            not isinstance(network_id, str)
            or CONTAINER_ID_PATTERN.fullmatch(network_id) is None
        ):
            _fail("stack network attachment ID is invalid")
    container_id = actual.get("container_id")
    if (
        not isinstance(container_id, str)
        or CONTAINER_ID_PATTERN.fullmatch(container_id) is None
    ):
        _fail("observed container ID is invalid")
    return container_id


def _validate_listeners(
    listeners: list[JsonObject],
    services: list[JsonObject],
) -> None:
    expected = sorted(
        (
            port["transport"],
            port["host"],
            port["port"],
            service["container_id"],
        )
        for service in services
        for port in _objects(service.get("published_ports"), "published ports")
    )
    actual: list[tuple[JsonValue, JsonValue, JsonValue, JsonValue]] = []
    for listener in listeners:
        if set(listener) != set(LISTENER_KEYS):
            _fail("stack listener has the wrong closed key set")
        socket_inode = listener.get("socket_inode")
        if isinstance(socket_inode, bool) or not isinstance(socket_inode, int):
            _fail("stack listener socket inode is invalid")
        if socket_inode < 1 or not _container_listener_nulls(listener):
            _fail("stack listener ownership mapping is invalid")
        actual.append(
            (
                listener.get("transport"),
                listener.get("host"),
                listener.get("port"),
                listener.get("container_id"),
            )
        )
    if actual != sorted(set(actual)) or actual != expected:
        _fail("stack listener mapping disagrees with published ports")


def _container_listener_nulls(listener: JsonObject) -> bool:
    return listener.get("owner_kind") == "container" and all(
        listener.get(key) is None
        for key in (
            "pid",
            "process_start_ticks",
            "executable_realpath",
            "argv_sha256",
        )
    )


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list):
        _fail(f"{context} must be an object array")
    result: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            _fail(f"{context} must be an object array")
        result.append(item)
    return result


def _json_strings(value: list[str]) -> list[JsonValue]:
    result: list[JsonValue] = []
    result.extend(value)
    return result
