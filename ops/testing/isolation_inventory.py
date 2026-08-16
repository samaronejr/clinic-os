"""Normalize the closed Docker and listener baseline used by the isolation ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Final, Never, cast

import rfc8785

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ops.testing.isolation_common import (  # noqa: E402, I001
    MAX_JSON_BYTES,
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
)
from ops.testing.isolation_inventory_values import (  # noqa: E402
    array,
    closed_object,
    entries,
    integer,
    json_array,
    ports,
    string_array,
    text,
)


ROOT_KEYS: Final = frozenset({"containers", "listeners", "networks", "volumes"})
CONTAINER_INPUT_KEYS: Final = frozenset(
    {
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
    }
)
VOLUME_KEYS: Final = frozenset(
    {"volume_name", "driver", "scope", "created_at", "mountpoint", "labels", "options"}
)
NETWORK_KEYS: Final = frozenset({"id", "name", "labels"})
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


def _fail(message: str) -> Never:
    raise IsolationError(message)


def normalize_inventory(value: object) -> JsonObject:
    """Validate an inventory fixture and return its exact ledger projection."""
    root = closed_object(value, ROOT_KEYS, "inventory")
    containers = [
        _normalize_container(item) for item in array(root["containers"], "containers")
    ]
    volumes = [_normalize_volume(item) for item in array(root["volumes"], "volumes")]
    networks = [
        _normalize_network(item) for item in array(root["networks"], "networks")
    ]
    listeners = [
        _normalize_listener(item) for item in array(root["listeners"], "listeners")
    ]
    containers.sort(key=_container_sort_key)
    listeners.sort(key=_listener_sort_key)
    networks.sort(key=_network_sort_key)
    volumes.sort(key=_volume_sort_key)
    return {
        "containers": json_array(containers),
        "listeners": json_array(listeners),
        "networks": json_array(networks),
        "volumes": json_array(volumes),
    }


def _normalize_container(value: object) -> JsonObject:
    item = closed_object(value, CONTAINER_INPUT_KEYS, "container")
    labels = entries(item["labels"], "container labels")
    published_ports = ports(item["published_ports"])
    mount_targets = sorted(set(string_array(item["mount_targets"], "mount targets")))
    if any(not target.startswith("/") for target in mount_targets):
        _fail("mount targets must be absolute")
    mount_target_values: list[JsonValue] = list(mount_targets)
    config: JsonObject = {
        "config_user": text(item["config_user"], "config user"),
        "image_id": text(item["image_id"], "image id"),
        "labels": labels,
        "mount_targets": mount_target_values,
        "network_mode": text(item["network_mode"], "network mode"),
        "published_ports": published_ports,
    }
    health = item["health"]
    if health is not None:
        health = text(health, "health")
    return {
        "config_sha256": hashlib.sha256(rfc8785.dumps(config)).hexdigest(),
        "health": health,
        "id": text(item["id"], "container id"),
        "labels": labels,
        "published_ports": published_ports,
        "restart_count": integer(item["restart_count"], "restart count", minimum=0),
        "state": text(item["state"], "state"),
    }


def _normalize_volume(value: object) -> JsonObject:
    item = closed_object(value, VOLUME_KEYS, "volume")
    return {
        "created_at": text(item["created_at"], "volume created_at"),
        "driver": text(item["driver"], "volume driver"),
        "labels": entries(item["labels"], "volume labels"),
        "mountpoint": text(item["mountpoint"], "volume mountpoint"),
        "options": entries(item["options"], "volume options"),
        "scope": text(item["scope"], "volume scope"),
        "volume_name": text(item["volume_name"], "volume name"),
    }


def _normalize_network(value: object) -> JsonObject:
    item = closed_object(value, NETWORK_KEYS, "network")
    return {
        "id": text(item["id"], "network id"),
        "labels": entries(item["labels"], "network labels"),
        "name": text(item["name"], "network name"),
    }


def _normalize_listener(value: object) -> JsonObject:
    item = closed_object(value, LISTENER_KEYS, "listener")
    owner_kind = text(item["owner_kind"], "listener owner kind")
    if owner_kind not in {"container", "process"}:
        _fail("listener owner kind must be container or process")
    nullable_text = ("container_id", "executable_realpath", "argv_sha256")
    normalized: JsonObject = {
        key: None if item[key] is None else text(item[key], f"listener {key}")
        for key in nullable_text
    }
    nullable_int = ("pid", "process_start_ticks")
    normalized.update(
        {
            key: None
            if item[key] is None
            else integer(item[key], f"listener {key}", minimum=0)
            for key in nullable_int
        }
    )
    normalized.update(
        {
            "host": text(item["host"], "listener host"),
            "owner_kind": owner_kind,
            "port": integer(item["port"], "listener port", minimum=1, maximum=65535),
            "socket_inode": integer(
                item["socket_inode"], "listener socket inode", minimum=1
            ),
            "transport": text(item["transport"], "listener transport"),
        }
    )
    if owner_kind == "container":
        valid = normalized["container_id"] is not None and all(
            normalized[key] is None
            for key in (
                "pid",
                "process_start_ticks",
                "executable_realpath",
                "argv_sha256",
            )
        )
    else:
        valid = normalized["container_id"] is None and all(
            normalized[key] is not None
            for key in (
                "pid",
                "process_start_ticks",
                "executable_realpath",
                "argv_sha256",
            )
        )
    if not valid:
        _fail("listener ownership fields do not match owner kind")
    return normalized


def _listener_sort_key(item: JsonObject) -> tuple[str, str, int, str, str, str, str]:
    return (
        text(item["transport"], "transport"),
        text(item["host"], "host"),
        integer(item["port"], "port"),
        text(item["owner_kind"], "owner kind"),
        ""
        if item["container_id"] is None
        else text(item["container_id"], "container id"),
        ""
        if item["executable_realpath"] is None
        else text(item["executable_realpath"], "executable"),
        "" if item["argv_sha256"] is None else text(item["argv_sha256"], "argv hash"),
    )


def _container_sort_key(item: JsonObject) -> str:
    return text(item["id"], "container id")


def _network_sort_key(item: JsonObject) -> tuple[str, str]:
    return (
        text(item["name"], "network name"),
        text(item["id"], "network id"),
    )


def _volume_sort_key(item: JsonObject) -> str:
    return text(item["volume_name"], "volume name")


def _load_fixture(path: Path) -> object:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        raw = os.read(descriptor, MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES or os.read(descriptor, 1):
            _fail("inventory fixture exceeds the size limit")
    finally:
        os.close(descriptor)
    try:
        return cast("object", json.loads(raw))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        message = f"invalid inventory fixture: {error}"
        raise IsolationError(message) from error


def _main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    normalize = subparsers.add_parser("normalize-fixture", allow_abbrev=False)
    normalize.add_argument("--input", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.command != "normalize-fixture":
        _fail("unsupported inventory command")
    normalized = normalize_inventory(arguments.input and _load_fixture(arguments.input))
    sys.stdout.buffer.write(canonical_bytes(normalized))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except IsolationError as error:
        message = f"isolation-inventory: {error}"
        raise SystemExit(message) from error
