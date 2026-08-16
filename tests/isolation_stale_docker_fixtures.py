from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject, JsonValue


class FakeDocker:
    def __init__(self) -> None:
        self.containers: dict[str, JsonObject] = {}
        self.networks: dict[str, JsonObject] = {}
        self.volumes: dict[str, JsonObject] = {}
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, arguments: tuple[str, ...]) -> str:
        self.calls.append(arguments)
        listings = {
            ("container", "ls", "--all", "--quiet", "--no-trunc"): self.containers,
            ("network", "ls", "--quiet", "--no-trunc"): self.networks,
            ("volume", "ls", "--quiet"): self.volumes,
        }
        listing = listings.get(arguments)
        if listing is not None:
            return _lines(listing)
        kind, command, *tail = arguments
        if command == "inspect":
            return self._inspect(kind, tail)
        return self._mutate(kind, command, tail, arguments)

    def _mutate(
        self,
        kind: str,
        command: str,
        tail: list[str],
        arguments: tuple[str, ...],
    ) -> str:
        if kind == "container" and command == "stop":
            self.containers[tail[-1]]["state"] = "exited"
            return tail[-1] + "\n"
        if kind == "container" and command == "rm":
            identifier = tail[-1]
            self.containers.pop(identifier)
            for network in self.networks.values():
                attachments = network["containers"]
                assert isinstance(attachments, list)
                if identifier in attachments:
                    attachments.remove(identifier)
            return identifier + "\n"
        if kind == "network" and command == "disconnect":
            network = self.networks[tail[-2]]
            attachments = network["containers"]
            assert isinstance(attachments, list)
            attachments.remove(tail[-1])
            return ""
        if kind == "network" and command == "rm":
            identifier = tail[-1]
            self.networks.pop(identifier)
            return identifier + "\n"
        if kind == "volume" and command == "rm":
            name = tail[-1]
            self.volumes.pop(name)
            return name + "\n"
        message = f"unexpected Docker command: {arguments!r}"
        raise AssertionError(message)

    def _inspect(self, kind: str, tail: list[str]) -> str:
        assert tail[0] == "--format"
        field = tail[1]
        identifier = tail[2]
        if kind == "container":
            value = self.containers[identifier]
            mapping = {
                "{{.Id}}": identifier,
                "{{.Name}}": "/" + str(value["name"]),
                "{{json .Config.Labels}}": json.dumps(value["labels"]),
                "{{.State.Status}}": value["state"],
            }
        elif kind == "network":
            value = self.networks[identifier]
            containers = value["containers"]
            assert isinstance(containers, list)
            mapping = {
                "{{.Id}}": identifier,
                "{{.Name}}": value["name"],
                "{{.Driver}}": value["driver"],
                "{{.Internal}}": str(value["internal"]).lower(),
                "{{.Attachable}}": str(value["attachable"]).lower(),
                "{{json .Labels}}": json.dumps(value["labels"]),
                "{{json .Containers}}": json.dumps(
                    {str(item): {} for item in containers}
                ),
            }
        else:
            value = self.volumes[identifier]
            mapping = {
                "{{.Name}}": identifier,
                "{{.Driver}}": value["driver"],
                "{{.Scope}}": value["scope"],
                "{{.CreatedAt}}": value["created_at"],
                "{{.Mountpoint}}": value["mountpoint"],
                "{{json .Labels}}": json.dumps(value["labels"]),
                "{{json .Options}}": json.dumps(value["options"]),
            }
        return str(mapping[field]) + "\n"


def container(
    identifier: str,
    *,
    name: str,
    labels: dict[str, str],
    state: str = "running",
) -> JsonObject:
    label_values = cast("JsonObject", dict(labels))
    return {"labels": label_values, "name": name, "state": state}


def network(identifier: str, *, name: str, containers: list[str]) -> JsonObject:
    container_values = cast("list[JsonValue]", list(containers))
    return {
        "attachable": False,
        "containers": container_values,
        "driver": "bridge",
        "id": identifier,
        "internal": True,
        "labels": {"clinic.phase1a.owner": "task"},
        "name": name,
    }


def volume(name: str) -> JsonObject:
    return {
        "created_at": "2026-07-16T21:00:00Z",
        "driver": "local",
        "labels": {"clinic.phase1a.owner": "task"},
        "mountpoint": f"/var/lib/docker/volumes/{name}/_data",
        "options": {},
        "scope": "local",
    }


def _lines(values: dict[str, JsonObject]) -> str:
    return "".join(f"{item}\n" for item in sorted(values))
