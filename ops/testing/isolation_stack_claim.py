"""Validate closed stack reservations and exclusive resource ownership."""

from __future__ import annotations

import re
from typing import Final, Never

from ops.testing.isolation_borrowed_resources import borrowed_resource_names
from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue
from ops.testing.isolation_stack_service import (
    validate_loopback_port,
    validate_stack_service,
)

DESIRED_KEYS: Final = frozenset(
    {
        "project",
        "services",
        "owned_volumes",
        "owned_networks",
        "database_names",
        "loopback_ports",
        "borrowed_volume_refs",
        "borrowed_network_refs",
    }
)
NAME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
LABEL_KEYS: Final = frozenset({"name", "value"})
OWNED_VOLUME_KEYS: Final = frozenset({"volume_name", "driver", "labels"})
OWNED_NETWORK_KEYS: Final = frozenset(
    {"network_name", "driver", "internal", "attachable", "labels"}
)


def _fail(message: str) -> Never:
    raise IsolationError(message)


def validate_stack_desired(value: JsonObject) -> None:
    """Validate the closed resource and service mapping of one stack spec."""
    if set(value) != set(DESIRED_KEYS):
        _fail("stack desired has the wrong closed key set")
    _ = _name(value["project"], "stack project")
    services = _objects(value["services"], "stack services")
    if not services:
        _fail("stack requires at least one desired service")
    owned_volumes = _owned_volumes(value["owned_volumes"])
    owned_networks = _owned_networks(value["owned_networks"])
    databases = _strings(value["database_names"], "database names")
    for database in databases:
        _ = _name(database, "database name")
    _sorted_unique(databases, "database names")
    ports = [
        validate_loopback_port(item, published=False)
        for item in _objects(value["loopback_ports"], "ports")
    ]
    _sorted_unique(ports, "loopback ports")
    names = [validate_stack_service(item, set(ports)) for item in services]
    _sorted_unique(names, "service names")
    borrowed_volumes = borrowed_resource_names(
        value["borrowed_volume_refs"], "volume_name", "read-only"
    )
    borrowed_networks = borrowed_resource_names(
        value["borrowed_network_refs"], "network_name", "attach"
    )
    if owned_volumes & borrowed_volumes or owned_networks & borrowed_networks:
        _fail("stack cannot own and borrow the same resource")
    _validate_service_resources(
        services,
        owned_volumes,
        owned_networks,
        borrowed_volumes,
        borrowed_networks,
    )


def reserved_stack_observed() -> JsonObject:
    """Return the exact empty observed identity set for a reserved stack."""
    return {
        "borrowed_networks": [],
        "borrowed_volumes": [],
        "container_ids": [],
        "listeners": [],
        "owned_networks": [],
        "owned_volumes": [],
        "services": [],
    }


def validate_stack_collisions(
    spec: JsonObject,
    ledger: JsonObject,
    claims: list[JsonObject],
) -> None:
    """Reject every reserved name or endpoint already owned or protected."""
    desired = _object(spec["desired"], "stack desired")
    projects = {_text(desired["project"], "stack project")}
    volumes = set(_resource_names(desired["owned_volumes"], "volume_name"))
    networks = set(_resource_names(desired["owned_networks"], "network_name"))
    databases = set(_strings(desired["database_names"], "database names"))
    ports = set(_port_keys(desired["loopback_ports"]))
    for claim in claims:
        if claim.get("kind") != "stack":
            continue
        existing = _object(claim["desired"], "existing stack desired")
        if _text(existing["project"], "existing project") in projects:
            _fail("stack project ownership collision")
        _reject_overlap(
            volumes, _resource_names(existing["owned_volumes"], "volume_name")
        )
        _reject_overlap(
            networks, _resource_names(existing["owned_networks"], "network_name")
        )
        _reject_overlap(
            databases, _strings(existing["database_names"], "database names")
        )
        _reject_overlap(ports, _port_keys(existing["loopback_ports"]))
    baseline = _object(ledger["baseline"], "ledger baseline")
    _reject_overlap(volumes, _baseline_names(baseline["volumes"], "volume_name"))
    _reject_overlap(networks, _baseline_names(baseline["networks"], "name"))
    _reject_overlap(ports, _baseline_ports(baseline["listeners"]))
    _reject_overlap(projects, _baseline_projects(baseline["containers"]))


def _owned_volumes(value: JsonValue) -> set[str]:
    items = _objects(value, "owned volumes")
    for item in items:
        if set(item) != set(OWNED_VOLUME_KEYS) or item.get("driver") != "local":
            _fail("owned volume has the wrong closed contract")
        _labels(item.get("labels"), "owned volume labels")
    names = _resource_names(value, "volume_name")
    _sorted_unique(names, "owned volumes")
    return set(names)


def _owned_networks(value: JsonValue) -> set[str]:
    items = _objects(value, "owned networks")
    for item in items:
        if (
            set(item) != set(OWNED_NETWORK_KEYS)
            or item.get("driver") != "bridge"
            or not isinstance(item.get("internal"), bool)
            or not isinstance(item.get("attachable"), bool)
        ):
            _fail("owned network has the wrong closed contract")
        _labels(item.get("labels"), "owned network labels")
    names = _resource_names(value, "network_name")
    _sorted_unique(names, "owned networks")
    return set(names)


def _labels(value: JsonValue, context: str) -> None:
    labels = _objects(value, context)
    names: list[str] = []
    for item in labels:
        if set(item) != set(LABEL_KEYS):
            _fail(f"{context} have the wrong closed key set")
        names.append(_text(item.get("name"), "label name"))
        _ = _text(item.get("value"), "label value")
    _sorted_unique(names, context)


def _validate_service_resources(
    services: list[JsonObject],
    owned_volumes: set[str],
    owned_networks: set[str],
    borrowed_volumes: set[str],
    borrowed_networks: set[str],
) -> None:
    for service in services:
        for reference in _objects(service.get("network_refs"), "network refs"):
            if reference.get("network_name") not in owned_networks | borrowed_networks:
                _fail("service network is not owned by its stack")
        for mount in _objects(service.get("volume_mounts"), "volume mounts"):
            volume_name = _text(mount.get("volume_name"), "mounted volume name")
            if volume_name in owned_volumes:
                continue
            if volume_name in borrowed_volumes and mount.get("read_only") is True:
                continue
            if not volume_name.startswith("/") or mount.get("read_only") is not True:
                _fail("service volume is not owned or an immutable bind source")


def _resource_names(value: JsonValue, key: str) -> list[str]:
    return [_text(item[key], key) for item in _objects(value, key)]


def _port_keys(value: JsonValue) -> list[tuple[str, str, int]]:
    return [
        validate_loopback_port(item, published=False)
        for item in _objects(value, "ports")
    ]


def _baseline_names(value: JsonValue, key: str) -> list[str]:
    return [_text(item[key], key) for item in _objects(value, "baseline resources")]


def _baseline_ports(value: JsonValue) -> list[tuple[str, str, int]]:
    return [
        (
            _text(item["transport"], "transport"),
            _text(item["host"], "host"),
            _integer(item["port"]),
        )
        for item in _objects(value, "baseline listeners")
    ]


def _baseline_projects(value: JsonValue) -> list[str]:
    return [
        _text(label["value"], "project label")
        for container in _objects(value, "baseline containers")
        for label in _objects(container["labels"], "container labels")
        if label.get("name") == "com.docker.compose.project"
    ]


def _reject_overlap[Sortable: (str, tuple[str, str, int])](
    left: set[Sortable],
    right: list[Sortable],
) -> None:
    if left.intersection(right):
        _fail("stack resource ownership collision")


def _sorted_unique[Sortable: (str, tuple[str, str, int])](
    values: list[Sortable],
    context: str,
) -> None:
    if values != sorted(set(values)):
        _fail(f"{context} are not sorted and unique")


def _name(value: JsonValue, context: str) -> str:
    text = _text(value, context)
    if NAME_PATTERN.fullmatch(text) is None:
        _fail(f"{context} is invalid")
    return text


def _integer(value: JsonValue) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("expected integer")
    return value


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


def _strings(value: JsonValue, context: str) -> list[str]:
    if not isinstance(value, list):
        _fail(f"{context} must be a string array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            _fail(f"{context} must be a string array")
        result.append(item)
    return result


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value
