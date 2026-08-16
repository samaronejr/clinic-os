"""Apply Docker mutations only after exact stale identity reinspection."""

from __future__ import annotations

from typing import Never, cast

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue
from ops.testing.isolation_docker_metadata import CommandRunner, run_docker_command
from ops.testing.isolation_stale_docker_inspection import (
    _NetworkIdentity,
    _VolumeIdentity,
    inspect_containers,
    inspect_networks,
    inspect_volumes,
)

STOPPABLE_STATES = frozenset({"paused", "restarting", "running"})
REMOVABLE_STATES = STOPPABLE_STATES | {"created", "dead", "exited"}


def execute_stale_docker_action(
    identity: JsonObject,
    runner: CommandRunner | None,
) -> None:
    """Dispatch one container, network, or volume action through a closed CLI."""
    run = run_docker_command if runner is None else runner
    operation = identity.get("operation")
    if operation == "stop-remove-container":
        _remove_container(identity, run)
    elif operation == "detach-borrowed-network":
        _detach_network(identity, run)
    elif operation == "remove-owned-network":
        _remove_network(identity, run)
    elif operation == "remove-owned-volume":
        _remove_volume(identity, run)
    else:
        _fail("stale Docker operation is invalid")


def _remove_container(identity: JsonObject, run: CommandRunner) -> None:
    expected_id = _optional_text(identity.get("container_id"), "container ID")
    expected_name = _optional_text(identity.get("container_name"), "container name")
    labels = _labels(identity.get("expected_labels"))
    exact = identity.get("expected_labels_exact") is True
    if expected_id is None and expected_name is None:
        _fail("container cleanup lacks an ID or deterministic name")
    matches = [
        item
        for item in inspect_containers(run)
        if item.identifier == expected_id
        or item.name == expected_name
        or _contains_labels(item.labels, labels)
    ]
    if not matches:
        return
    if len(matches) != 1:
        _fail("container cleanup selector union is ambiguous")
    container = matches[0]
    labels_match = (
        container.labels == labels
        if exact
        else _contains_labels(container.labels, labels)
    )
    if (
        (expected_id is not None and container.identifier != expected_id)
        or (expected_name is not None and container.name != expected_name)
        or not labels_match
    ):
        _fail("container cleanup selector union disagrees")
    if container.state not in REMOVABLE_STATES:
        _fail("container state is not cleanup-authorized")
    if container.state in STOPPABLE_STATES:
        run(("container", "stop", "--time", "10", container.identifier))
    run(("container", "rm", container.identifier))
    if any(
        item.identifier == expected_id
        or item.name == expected_name
        or _contains_labels(item.labels, labels)
        for item in inspect_containers(run)
    ):
        _fail("container cleanup selector union remains present")


def _detach_network(identity: JsonObject, run: CommandRunner) -> None:
    expected = _network_observation(identity)
    network = _select_network(expected, inspect_networks(run), absent_ok=False)
    if network is None:
        _fail("borrowed network disappeared before owner cleanup")
    container_ids = _strings(identity.get("container_ids"), "container IDs")
    for container_id in container_ids:
        if container_id in network.containers:
            run(("network", "disconnect", "--force", network.identifier, container_id))
    refreshed = _select_network(expected, inspect_networks(run), absent_ok=False)
    if refreshed is None or set(container_ids).intersection(refreshed.containers):
        _fail("borrowed network attachment remains present")


def _remove_network(identity: JsonObject, run: CommandRunner) -> None:
    expected = _network_observation(identity)
    network = _select_network(expected, inspect_networks(run), absent_ok=True)
    if network is None:
        return
    if network.containers:
        _fail("owned network still has container attachments")
    run(("network", "rm", network.identifier))
    if _select_network(expected, inspect_networks(run), absent_ok=True) is not None:
        _fail("owned network remains after removal")


def _remove_volume(identity: JsonObject, run: CommandRunner) -> None:
    expected = _object(identity.get("observed_resource"), "volume observation")
    name = _text(expected.get("volume_name"), "volume name")
    matches = [item for item in inspect_volumes(run) if item.name == name]
    if not matches:
        return
    if len(matches) != 1 or not _volume_matches(matches[0], expected):
        _fail("volume identity drifted before cleanup")
    run(("volume", "rm", name))
    if any(item.name == name for item in inspect_volumes(run)):
        _fail("owned volume remains after removal")


def _select_network(
    expected: JsonObject,
    networks: list[_NetworkIdentity],
    *,
    absent_ok: bool,
) -> _NetworkIdentity | None:
    identifier = _text(expected.get("network_id"), "network ID")
    name = _text(expected.get("network_name"), "network name")
    matches = [
        item for item in networks if item.identifier == identifier or item.name == name
    ]
    if not matches:
        if absent_ok:
            return None
        _fail("network identity is absent")
    if len(matches) != 1 or not _network_matches(matches[0], expected):
        _fail("network identity drifted before cleanup")
    return matches[0]


def _network_matches(actual: _NetworkIdentity, expected: JsonObject) -> bool:
    return (
        actual.identifier == expected.get("network_id")
        and actual.name == expected.get("network_name")
        and actual.driver == expected.get("driver")
        and actual.internal == expected.get("internal")
        and actual.attachable == expected.get("attachable")
        and actual.labels == _labels(expected.get("labels"))
    )


def _volume_matches(actual: _VolumeIdentity, expected: JsonObject) -> bool:
    return (
        actual.driver == expected.get("driver")
        and actual.scope == expected.get("scope")
        and actual.created_at == expected.get("created_at")
        and actual.mountpoint == expected.get("mountpoint")
        and actual.labels == _labels(expected.get("labels"))
        and actual.options == _labels(expected.get("options"))
    )


def _network_observation(identity: JsonObject) -> JsonObject:
    return _object(identity.get("observed_resource"), "network observation")


def _labels(value: JsonValue) -> dict[str, str]:
    entries = _objects(value, "labels")
    result = {
        _text(item.get("name"), "label name"): _text(item.get("value"), "label value")
        for item in entries
    }
    if len(result) != len(entries):
        _fail("Docker labels are duplicated")
    return result


def _contains_labels(actual: dict[str, str], expected: dict[str, str]) -> bool:
    return all(actual.get(key) == value for key, value in expected.items())


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} must be an object array")
    return cast("list[JsonObject]", value)


def _strings(value: JsonValue, context: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _fail(f"{context} must be a string array")
    result = cast("list[str]", value)
    if result != sorted(set(result)):
        _fail(f"{context} must be sorted and unique")
    return result


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _optional_text(value: JsonValue, context: str) -> str | None:
    if value is not None and not isinstance(value, str):
        _fail(f"{context} must be a string or null")
    return value


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
