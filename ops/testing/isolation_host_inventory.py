"""Capture plan-permitted Docker and host-listener metadata without mutation."""

from __future__ import annotations

from pathlib import Path
from typing import Final, Never

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue
from ops.testing.isolation_docker_metadata import (
    capture_docker_attestation_metadata,
    capture_docker_metadata,
)
from ops.testing.isolation_inventory import normalize_inventory
from ops.testing.isolation_listener_metadata import capture_loopback_listeners

CONTAINER_PROJECTION_KEYS: Final = (
    "config_user",
    "health",
    "id",
    "image_id",
    "labels",
    "mount_targets",
    "network_mode",
    "published_ports",
    "restart_count",
    "state",
)
NETWORK_PROJECTION_KEYS: Final = ("id", "labels", "name")


def _fail(message: str) -> Never:
    raise IsolationError(message)


def capture_host_inventory() -> JsonObject:
    """Read the closed live-host baseline required by first-ledger creation."""
    return project_host_inventory(_with_listeners(capture_docker_metadata()))


def capture_live_host_inventory() -> JsonObject:
    """Read the richer live metadata needed to attest an existing claim."""
    result = _with_listeners(capture_docker_attestation_metadata())
    result["attestation_version"] = 1
    return result


def project_host_inventory(value: JsonObject) -> JsonObject:
    """Remove attestation-only metadata before ledger baseline comparison."""
    containers = _project_objects(
        value.get("containers"),
        CONTAINER_PROJECTION_KEYS,
        "container",
    )
    networks = _project_objects(
        value.get("networks"),
        NETWORK_PROJECTION_KEYS,
        "network",
    )
    return normalize_inventory(
        {
            "containers": _json_objects(containers),
            "listeners": value.get("listeners"),
            "networks": _json_objects(networks),
            "volumes": value.get("volumes"),
        }
    )


def _with_listeners(docker: JsonObject) -> JsonObject:
    listeners = capture_loopback_listeners(
        Path("/proc"),
        _json_objects(_object_array(docker["containers"])),
    )
    return {
        "containers": docker["containers"],
        "listeners": listeners,
        "networks": docker["networks"],
        "volumes": docker["volumes"],
    }


def _object_array(value: JsonValue) -> list[JsonObject]:
    if not isinstance(value, list):
        _fail("Docker container inventory is not an object array")
    result: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            _fail("Docker container inventory is not an object array")
        result.append(item)
    return result


def _project_objects(
    value: JsonValue,
    keys: tuple[str, ...],
    context: str,
) -> list[JsonObject]:
    result: list[JsonObject] = []
    for item in _object_array(value):
        if any(key not in item for key in keys):
            _fail(f"Docker {context} projection is incomplete")
        result.append({key: item[key] for key in keys})
    return result


def _json_objects(value: list[JsonObject]) -> list[JsonValue]:
    result: list[JsonValue] = []
    result.extend(value)
    return result
