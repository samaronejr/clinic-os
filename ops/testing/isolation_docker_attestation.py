"""Project field-specific Docker metadata needed for live claim attestation."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Never

from typing_extensions import TypeIs

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue

type CommandRunner = Callable[[tuple[str, ...]], str]
JSON_LOADER: Callable[[str], object] = json.loads

MOUNT_TEMPLATE = (
    '{{range .Mounts}}{{printf "%s|%s|%s|%s|%t\\n" '
    ".Type .Name .Source .Destination .RW}}{{end}}"
)
MOUNT_FIELD_COUNT = 5
NETWORK_TEMPLATE = (
    '{{range $name, $settings := .NetworkSettings.Networks}}{{printf "%s|%s|" '
    "$name $settings.NetworkID}}{{range $settings.Aliases}}"
    '{{printf "%s," .}}{{end}}{{println}}{{end}}'
)
NETWORK_FIELD_COUNT = 3


def enrich_docker_attestation(
    base: JsonObject,
    run: CommandRunner,
) -> JsonObject:
    """Return one copy enriched by exact field-specific inspect results."""
    result = copy.deepcopy(base)
    containers = _objects(result.get("containers"), "containers")
    for container in containers:
        identifier = _text(container.get("id"), "container ID")
        container["command"] = _command(
            run(
                (
                    "container",
                    "inspect",
                    "--format",
                    "{{json .Config.Cmd}}",
                    identifier,
                )
            )
        )
        container["mounts"] = _mounts(
            run(("container", "inspect", "--format", MOUNT_TEMPLATE, identifier))
        )
        container["network_attachments"] = _network_attachments(
            run(("container", "inspect", "--format", NETWORK_TEMPLATE, identifier))
        )
    networks = _objects(result.get("networks"), "networks")
    for network in networks:
        identifier = _text(network.get("id"), "network ID")
        network["driver"] = run(
            ("network", "inspect", "--format", "{{.Driver}}", identifier)
        ).strip()
        network["internal"] = _boolean(
            run(("network", "inspect", "--format", "{{.Internal}}", identifier))
        )
        network["attachable"] = _boolean(
            run(("network", "inspect", "--format", "{{.Attachable}}", identifier))
        )
    return result


def _command(raw: str) -> list[JsonValue]:
    try:
        return _command_value(JSON_LOADER(raw))
    except json.JSONDecodeError as error:
        message = "invalid container command JSON"
        raise IsolationError(message) from error


def _command_value(value: object) -> list[JsonValue]:
    if not _is_object_list(value):
        _fail("container command must be a string array")
    result: list[JsonValue] = []
    for item in value:
        if not isinstance(item, str):
            _fail("container command must be a string array")
        result.append(item)
    if not result or any(not item for item in result):
        _fail("container command is empty")
    return result


def _mounts(raw: str) -> list[JsonValue]:
    result: list[JsonObject] = []
    for line in (item for item in raw.splitlines() if item):
        parts = line.split("|")
        if len(parts) != MOUNT_FIELD_COUNT:
            _fail("invalid container mount metadata")
        kind, name, source, target, writable = parts
        if kind not in {"bind", "volume"} or not target.startswith("/"):
            _fail("container mount metadata is outside the closed contract")
        selected_source = name if kind == "volume" else source
        if not selected_source:
            _fail("container mount source is empty")
        result.append(
            {
                "read_only": not _boolean(writable),
                "source": selected_source,
                "target": target,
                "type": kind,
            }
        )
    result.sort(key=lambda item: (str(item["target"]), str(item["source"])))
    return _json_objects(result)


def _network_attachments(raw: str) -> list[JsonValue]:
    result: list[JsonObject] = []
    for line in (item for item in raw.splitlines() if item):
        parts = line.split("|")
        if len(parts) != NETWORK_FIELD_COUNT:
            _fail("invalid container network metadata")
        name, identifier, aliases_raw = parts
        aliases = sorted({item for item in aliases_raw.split(",") if item})
        if not name or not identifier:
            _fail("container network attachment is incomplete")
        alias_values: list[JsonValue] = []
        alias_values.extend(aliases)
        result.append(
            {
                "aliases": alias_values,
                "network_id": identifier,
                "network_name": name,
            }
        )
    result.sort(key=lambda item: str(item["network_name"]))
    return _json_objects(result)


def _boolean(raw: str) -> bool:
    value = raw.strip()
    if value == "true":
        return True
    if value == "false":
        return False
    return _fail("Docker boolean field is invalid")


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list):
        _fail(f"Docker {context} are not objects")
    result: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            _fail(f"Docker {context} are not objects")
        result.append(item)
    return result


def _json_objects(value: list[JsonObject]) -> list[JsonValue]:
    result: list[JsonValue] = []
    result.extend(value)
    return result


def _is_object_list(value: object) -> TypeIs[list[object]]:
    return isinstance(value, list)


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{context} is invalid")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
