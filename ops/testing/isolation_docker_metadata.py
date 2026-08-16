"""Read only plan-permitted Docker list and field-specific inspect metadata."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Final, Never, cast

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue
from ops.testing.isolation_docker_attestation import enrich_docker_attestation

CommandRunner = Callable[[tuple[str, ...]], str]
DOCKER_TIMEOUT_SECONDS: Final = 30
MAX_OUTPUT_BYTES: Final = 16 * 1024 * 1024
HEX64: Final = re.compile(r"^[0-9a-f]{64}$")
ENTRY_KEYS: Final = frozenset({"HostIp", "HostPort"})


def _fail(message: str) -> Never:
    raise IsolationError(message)


def capture_docker_metadata(runner: CommandRunner | None = None) -> JsonObject:
    """Enumerate containers, volumes, and networks through the closed read API."""
    run = run_docker_command if runner is None else runner
    containers: list[JsonValue] = [
        _container(run, identifier)
        for identifier in _identifiers(
            run(("container", "ls", "--all", "--quiet", "--no-trunc")),
            "container",
        )
    ]
    volumes: list[JsonValue] = [
        _volume(run, name)
        for name in _names(run(("volume", "ls", "--quiet")), "volume")
    ]
    networks: list[JsonValue] = [
        _network(run, identifier)
        for identifier in _identifiers(
            run(("network", "ls", "--quiet", "--no-trunc")),
            "network",
        )
    ]
    return {"containers": containers, "networks": networks, "volumes": volumes}


def capture_docker_attestation_metadata(
    runner: CommandRunner | None = None,
) -> JsonObject:
    """Add only exact service mappings needed to attest reserved live resources."""
    run = run_docker_command if runner is None else runner
    return enrich_docker_attestation(capture_docker_metadata(run), run)


def _container(run: CommandRunner, identifier: str) -> JsonObject:
    container_id = _inspect(run, "container", identifier, "{{.Id}}")
    if container_id != identifier:
        _fail("container list/inspect identity mismatch")
    health = _inspect(
        run,
        "container",
        identifier,
        "{{if .State.Health}}{{.State.Health.Status}}{{end}}",
    )
    mount_targets = sorted(
        set(
            _inspect(
                run,
                "container",
                identifier,
                "{{range .Mounts}}{{println .Destination}}{{end}}",
            ).splitlines()
        )
        - {""}
    )
    mount_target_values: list[JsonValue] = list(mount_targets)
    return {
        "config_user": _inspect(run, "container", identifier, "{{.Config.User}}"),
        "health": health or None,
        "id": container_id,
        "image_id": _inspect(run, "container", identifier, "{{.Image}}"),
        "labels": _mapping_entries(
            _inspect(run, "container", identifier, "{{json .Config.Labels}}"),
            "container labels",
        ),
        "mount_targets": mount_target_values,
        "network_mode": _inspect(
            run,
            "container",
            identifier,
            "{{.HostConfig.NetworkMode}}",
        ),
        "published_ports": _published_ports(
            _inspect(
                run,
                "container",
                identifier,
                "{{json .NetworkSettings.Ports}}",
            )
        ),
        "restart_count": _nonnegative(
            _inspect(run, "container", identifier, "{{.RestartCount}}"),
            "restart count",
        ),
        "state": _inspect(run, "container", identifier, "{{.State.Status}}"),
    }


def _volume(run: CommandRunner, name: str) -> JsonObject:
    observed_name = _inspect(run, "volume", name, "{{.Name}}")
    if observed_name != name:
        _fail("volume list/inspect identity mismatch")
    return {
        "created_at": _inspect(run, "volume", name, "{{.CreatedAt}}"),
        "driver": _inspect(run, "volume", name, "{{.Driver}}"),
        "labels": _mapping_entries(
            _inspect(run, "volume", name, "{{json .Labels}}"),
            "volume labels",
        ),
        "mountpoint": _inspect(run, "volume", name, "{{.Mountpoint}}"),
        "options": _mapping_entries(
            _inspect(run, "volume", name, "{{json .Options}}"),
            "volume options",
        ),
        "scope": _inspect(run, "volume", name, "{{.Scope}}"),
        "volume_name": observed_name,
    }


def _network(run: CommandRunner, identifier: str) -> JsonObject:
    network_id = _inspect(run, "network", identifier, "{{.Id}}")
    if network_id != identifier:
        _fail("network list/inspect identity mismatch")
    return {
        "id": network_id,
        "labels": _mapping_entries(
            _inspect(run, "network", identifier, "{{json .Labels}}"),
            "network labels",
        ),
        "name": _inspect(run, "network", identifier, "{{.Name}}"),
    }


def _inspect(run: CommandRunner, kind: str, identifier: str, field: str) -> str:
    return run((kind, "inspect", "--format", field, identifier)).strip()


def _published_ports(raw: str) -> list[JsonValue]:
    value = _json_value(raw, "published ports")
    if value is None:
        return []
    mapping = _object(value, "published ports")
    ports: list[JsonValue] = []
    for container_binding, raw_bindings in mapping.items():
        container_text, separator, transport = container_binding.rpartition("/")
        if separator != "/" or transport not in {"tcp", "udp"}:
            _fail("invalid container port binding")
        container_port = _nonnegative(container_text, "container port")
        if raw_bindings is None:
            continue
        if not isinstance(raw_bindings, list):
            _fail("published port bindings must be an array or null")
        for raw_binding in raw_bindings:
            binding = _closed_object(raw_binding, ENTRY_KEYS, "published port binding")
            ports.append(
                {
                    "container_port": container_port,
                    "host": _string(binding["HostIp"], "published host"),
                    "port": _nonnegative(
                        _string(binding["HostPort"], "published port"),
                        "published port",
                    ),
                    "transport": transport,
                }
            )
    return ports


def _mapping_entries(raw: str, context: str) -> list[JsonValue]:
    value = _json_value(raw, context)
    if value is None:
        return []
    mapping = _object(value, context)
    return [
        {"name": name, "value": _string(entry, context)}
        for name, entry in sorted(mapping.items())
    ]


def _json_value(raw: str, context: str) -> object:
    try:
        return cast("object", json.loads(raw))
    except json.JSONDecodeError as error:
        message = f"invalid {context} JSON"
        raise IsolationError(message) from error


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _fail(f"{context} must be an object")
    return {str(key): entry for key, entry in value.items()}


def _closed_object(
    value: object,
    keys: frozenset[str],
    context: str,
) -> dict[str, object]:
    item = _object(value, context)
    if item.keys() != keys:
        _fail(f"{context} has unknown or missing fields")
    return item


def _identifiers(raw: str, context: str) -> list[str]:
    values = _names(raw, context)
    if any(HEX64.fullmatch(value) is None for value in values):
        _fail(f"invalid {context} identifier")
    return values


def _names(raw: str, context: str) -> list[str]:
    values = sorted(raw.splitlines())
    if any(not value or "\x00" in value or "\n" in value for value in values):
        _fail(f"invalid {context} name")
    if len(values) != len(set(values)):
        _fail(f"duplicate {context} name")
    return values


def _nonnegative(raw: str, context: str) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        message = f"{context} must be an integer"
        raise IsolationError(message) from error
    if value < 0:
        _fail(f"{context} must be nonnegative")
    return value


def _string(value: object, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def run_docker_command(arguments: tuple[str, ...]) -> str:
    """Run one closed Docker CLI tuple with bounded captured output."""
    executable = shutil.which("docker")
    if executable is None:
        _fail("docker executable is unavailable")
    docker = Path(executable).resolve(strict=True)
    completed = subprocess.run(  # noqa: S603 - resolved executable and closed arguments.
        (str(docker), *arguments),
        check=False,
        capture_output=True,
        text=True,
        timeout=DOCKER_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        _fail("Docker metadata command failed")
    if len(completed.stdout.encode()) > MAX_OUTPUT_BYTES:
        _fail("Docker metadata output exceeds the size limit")
    return completed.stdout
