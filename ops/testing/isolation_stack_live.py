"""Project stack observations from the closed live host inventory."""

from __future__ import annotations

import copy
from typing import Never

from ops.testing.isolation_borrowed_resources import observe_borrowed_resources
from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue
from ops.testing.isolation_stack_observation import validate_stack_observation
from ops.testing.isolation_stack_service_live import observe_live_service


def observe_live_stack(
    claim: JsonObject,
    inventory: JsonObject,
) -> JsonObject | None:
    """Return an exact complete stack observation, absence, or fail on partials."""
    desired = _object(claim.get("desired"), "stack desired")
    project = _text(desired.get("project"), "stack project")
    strict_attestation = inventory.get("attestation_version") == 1
    services = _objects(desired.get("services"), "desired services")
    containers = _objects(inventory.get("containers"), "live containers")
    observed_services: list[JsonObject] = []
    missing = 0
    for service in services:
        service_name = _text(service.get("name"), "service name")
        matches = [
            item
            for item in containers
            if _has_label(item, "com.docker.compose.project", project)
            and _has_label(item, "com.docker.compose.service", service_name)
        ]
        if not matches:
            missing += 1
            continue
        if len(matches) != 1:
            _fail("live stack service identity is ambiguous")
        observed_services.append(
            observe_live_service(service, matches[0], strict=strict_attestation)
        )
    if missing == len(services):
        return None
    if missing:
        _fail("live stack reservation is partial")
    borrowed_volumes, borrowed_networks = observe_borrowed_resources(
        desired,
        inventory,
    )
    container_ids = sorted(
        _text(item.get("container_id"), "observed container ID")
        for item in observed_services
    )
    listeners = [
        copy.deepcopy(item)
        for item in _objects(inventory.get("listeners"), "live listeners")
        if item.get("container_id") in container_ids
    ]
    listeners.sort(
        key=lambda item: (
            str(item.get("transport")),
            str(item.get("host")),
            _integer(item.get("port"), "listener port"),
        )
    )
    observed: JsonObject = {
        "borrowed_networks": _json_objects(borrowed_networks),
        "borrowed_volumes": _json_objects(borrowed_volumes),
        "container_ids": _json_strings(container_ids),
        "listeners": _json_objects(listeners),
        "owned_networks": _json_objects(_owned_networks(desired, inventory)),
        "owned_volumes": _json_objects(_owned_volumes(desired, inventory)),
        "services": _json_objects(observed_services),
    }
    validate_stack_observation(observed, claim)
    return observed


def _owned_volumes(desired: JsonObject, inventory: JsonObject) -> list[JsonObject]:
    current = _objects(inventory.get("volumes"), "live volumes")
    result: list[JsonObject] = []
    for expected in _objects(desired.get("owned_volumes"), "owned volumes"):
        name = _text(expected.get("volume_name"), "owned volume name")
        matches = [item for item in current if item.get("volume_name") == name]
        if len(matches) != 1:
            _fail("owned volume is missing or ambiguous")
        actual = matches[0]
        if actual.get("driver") != expected.get("driver"):
            _fail("owned volume identity drifted")
        recorded = copy.deepcopy(actual)
        recorded["labels"] = _selected_labels(actual, expected)
        result.append(recorded)
    return result


def _owned_networks(desired: JsonObject, inventory: JsonObject) -> list[JsonObject]:
    current = _objects(inventory.get("networks"), "live networks")
    result: list[JsonObject] = []
    for expected in _objects(desired.get("owned_networks"), "owned networks"):
        name = _text(expected.get("network_name"), "owned network name")
        matches = [item for item in current if item.get("name") == name]
        if len(matches) != 1:
            _fail("owned network is missing or ambiguous")
        actual = matches[0]
        for field in ("driver", "internal", "attachable"):
            if actual.get(field) != expected.get(field):
                _fail(f"owned network {field} drifted")
        result.append(
            {
                "attachable": actual.get("attachable"),
                "driver": actual.get("driver"),
                "internal": actual.get("internal"),
                "labels": _selected_labels(actual, expected),
                "network_id": actual.get("id"),
                "network_name": name,
            }
        )
    return result


def _selected_labels(actual: JsonObject, expected: JsonObject) -> list[JsonValue]:
    current = _objects(actual.get("labels"), "live resource labels")
    selected = _objects(expected.get("labels"), "desired resource labels")
    if any(item not in current for item in selected):
        _fail("owned resource intent labels drifted")
    return _json_objects(selected)


def _has_label(container: JsonObject, name: str, value: str) -> bool:
    return any(
        item.get("name") == name and item.get("value") == value
        for item in _objects(container.get("labels"), "container labels")
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


def _json_objects(value: list[JsonObject]) -> list[JsonValue]:
    return [copy.deepcopy(item) for item in value]


def _json_strings(value: list[str]) -> list[JsonValue]:
    result: list[JsonValue] = []
    result.extend(value)
    return result


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _integer(value: JsonValue, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail(f"{context} must be a positive integer")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
