"""Read the Docker identity fields used by stale physical cleanup."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Never, cast

from ops.testing.isolation_common import IsolationError

if TYPE_CHECKING:
    from ops.testing.isolation_docker_metadata import CommandRunner

HEX64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class _ContainerIdentity:
    identifier: str
    name: str
    labels: dict[str, str]
    state: str


@dataclass(frozen=True, slots=True)
class _NetworkIdentity:
    identifier: str
    name: str
    driver: str
    internal: bool
    attachable: bool
    labels: dict[str, str]
    containers: frozenset[str]


@dataclass(frozen=True, slots=True)
class _VolumeIdentity:
    name: str
    driver: str
    scope: str
    created_at: str
    mountpoint: str
    labels: dict[str, str]
    options: dict[str, str]


def inspect_containers(run: CommandRunner) -> list[_ContainerIdentity]:
    """Return every container's nonsecret cleanup selectors."""
    identifiers = _identifiers(
        run(("container", "ls", "--all", "--quiet", "--no-trunc")),
        "container",
    )
    return [
        _ContainerIdentity(
            identifier=_same_identifier(
                identifier,
                _inspect(run, "container", identifier, "{{.Id}}"),
                "container",
            ),
            name=_inspect(run, "container", identifier, "{{.Name}}").removeprefix("/"),
            labels=_mapping(
                _inspect(run, "container", identifier, "{{json .Config.Labels}}"),
                "container labels",
            ),
            state=_inspect(run, "container", identifier, "{{.State.Status}}"),
        )
        for identifier in identifiers
    ]


def inspect_networks(run: CommandRunner) -> list[_NetworkIdentity]:
    """Return every network's deletion identity and attachment set."""
    identifiers = _identifiers(
        run(("network", "ls", "--quiet", "--no-trunc")),
        "network",
    )
    return [
        _NetworkIdentity(
            identifier=_same_identifier(
                identifier,
                _inspect(run, "network", identifier, "{{.Id}}"),
                "network",
            ),
            name=_inspect(run, "network", identifier, "{{.Name}}"),
            driver=_inspect(run, "network", identifier, "{{.Driver}}"),
            internal=_boolean(
                _inspect(run, "network", identifier, "{{.Internal}}"),
                "network internal",
            ),
            attachable=_boolean(
                _inspect(run, "network", identifier, "{{.Attachable}}"),
                "network attachable",
            ),
            labels=_mapping(
                _inspect(run, "network", identifier, "{{json .Labels}}"),
                "network labels",
            ),
            containers=frozenset(
                _mapping(
                    _inspect(run, "network", identifier, "{{json .Containers}}"),
                    "network containers",
                    require_text_values=False,
                )
            ),
        )
        for identifier in identifiers
    ]


def inspect_volumes(run: CommandRunner) -> list[_VolumeIdentity]:
    """Return every volume's exact Docker metadata identity."""
    names = _names(run(("volume", "ls", "--quiet")), "volume")
    return [
        _VolumeIdentity(
            name=_same_identifier(
                name,
                _inspect(run, "volume", name, "{{.Name}}"),
                "volume",
            ),
            driver=_inspect(run, "volume", name, "{{.Driver}}"),
            scope=_inspect(run, "volume", name, "{{.Scope}}"),
            created_at=_inspect(run, "volume", name, "{{.CreatedAt}}"),
            mountpoint=_inspect(run, "volume", name, "{{.Mountpoint}}"),
            labels=_mapping(
                _inspect(run, "volume", name, "{{json .Labels}}"),
                "volume labels",
            ),
            options=_mapping(
                _inspect(run, "volume", name, "{{json .Options}}"),
                "volume options",
            ),
        )
        for name in names
    ]


def _inspect(
    run: CommandRunner,
    kind: str,
    identifier: str,
    field: str,
) -> str:
    return run((kind, "inspect", "--format", field, identifier)).strip()


def _mapping(
    raw: str,
    context: str,
    *,
    require_text_values: bool = True,
) -> dict[str, str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        message = f"invalid {context} JSON"
        raise IsolationError(message) from error
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _fail(f"{context} must be an object")
    if require_text_values and not all(
        isinstance(item, str) for item in value.values()
    ):
        _fail(f"{context} values must be strings")
    mapping = cast("dict[str, object]", value)
    return {str(key): str(item) for key, item in mapping.items()}


def _identifiers(raw: str, context: str) -> list[str]:
    values = _names(raw, context)
    if any(HEX64.fullmatch(value) is None for value in values):
        _fail(f"invalid {context} identifier")
    return values


def _names(raw: str, context: str) -> list[str]:
    values = sorted(raw.splitlines())
    if any(not item or "\x00" in item for item in values):
        _fail(f"invalid {context} name")
    if len(values) != len(set(values)):
        _fail(f"duplicate {context} name")
    return values


def _same_identifier(expected: str, actual: str, context: str) -> str:
    if actual != expected:
        _fail(f"{context} list/inspect identity mismatch")
    return actual


def _boolean(raw: str, context: str) -> bool:
    if raw not in {"true", "false"}:
        _fail(f"{context} is not Boolean")
    return raw == "true"


def _fail(message: str) -> Never:
    raise IsolationError(message)
