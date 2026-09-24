from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable

    from ops.testing.isolation_common import JsonObject


class FakeRunnerDocker:
    def __init__(self) -> None:
        self.containers: dict[str, JsonObject] = {}
        self.calls: list[tuple[str, ...]] = []
        self.on_create: Callable[[tuple[str, ...]], JsonObject] | None = None
        self.before_mutation: Callable[[], None] | None = None

    def __call__(self, arguments: tuple[str, ...]) -> str:
        self.calls.append(arguments)
        if arguments == ("container", "ls", "--all", "--quiet", "--no-trunc"):
            return "".join(f"{item}\n" for item in sorted(self.containers))
        if arguments[0] == "create":
            assert self.on_create is not None
            value = self.on_create(arguments)
            identifier = str(value["id"])
            self.containers[identifier] = value
            return identifier + "\n"
        kind, command, *tail = arguments
        assert kind == "container"
        if command == "inspect":
            return self._inspect(tail)
        if self.before_mutation is not None:
            self.before_mutation()
        identifier = tail[-1]
        if command == "stop":
            self.containers[identifier]["state"] = "exited"
            self.containers[identifier]["pid"] = 0
            return identifier + "\n"
        if command == "rm":
            self.containers.pop(identifier)
            return identifier + "\n"
        message = f"unexpected Docker command: {arguments!r}"
        raise AssertionError(message)

    def _inspect(self, tail: list[str]) -> str:
        assert tail[0] == "--format"
        field, identifier = tail[1:]
        value = self.containers[identifier]
        fields: dict[str, object] = {
            "{{.Id}}": identifier,
            "{{.Name}}": "/" + str(value["name"]),
            "{{json .Config.Labels}}": json.dumps(value["labels"]),
            "{{.State.Status}}": value["state"],
            "{{.State.Pid}}": value["pid"],
            "{{.RestartCount}}": value["restart_count"],
            "{{.Image}}": value["image_id"],
            "{{.Config.User}}": value["user"],
            "{{json .Config.Cmd}}": json.dumps(value["command"]),
            "{{json .Config.Env}}": json.dumps(value["environment"]),
            "{{.HostConfig.NetworkMode}}": value["network_mode"],
            "{{.HostConfig.ReadonlyRootfs}}": str(value["read_only"]).lower(),
            "{{json .HostConfig.Tmpfs}}": json.dumps(value["tmpfs"]),
            "{{.HostConfig.IpcMode}}": value["ipc_mode"],
            "{{.HostConfig.ShmSize}}": value["shm_size"],
            "{{json .HostConfig.ExtraHosts}}": json.dumps(value["extra_hosts"]),
            "{{json .Mounts}}": json.dumps(value["mounts"]),
            "{{json .HostConfig.PortBindings}}": json.dumps(value["ports"]),
            "{{.HostConfig.AutoRemove}}": str(value["auto_remove"]).lower(),
            "{{.Config.OpenStdin}}": str(value["open_stdin"]).lower(),
            "{{.Config.AttachStdin}}": str(value["attach_stdin"]).lower(),
        }
        return str(fields[field]) + "\n"


def exact_runner_container(
    ledger: JsonObject,
    *,
    state: str = "created",
    identifier: str = "e" * 64,
) -> JsonObject:
    claims = ledger["claims"]
    assert isinstance(claims, list)
    assert isinstance(claims[0], dict)
    claim = claims[0]
    creation = claim["runner_creation"]
    desired = claim["desired"]
    assert isinstance(creation, dict)
    assert isinstance(desired, dict)
    services = desired["services"]
    assert isinstance(services, list)
    assert isinstance(services[0], dict)
    service = services[0]
    environment = service["environment_contract"]
    assert isinstance(environment, dict)
    literal = environment["literal"]
    assert isinstance(literal, list)
    assert all(isinstance(item, dict) for item in literal)
    literal_entries = cast("list[JsonObject]", literal)
    return {
        "attach_stdin": True,
        "auto_remove": True,
        "command": service["command"],
        "environment": [f"{item['name']}={item['value']}" for item in literal_entries],
        "extra_hosts": [],
        "id": identifier,
        "image_id": service["image_id"],
        "ipc_mode": "private",
        "labels": {
            "clinic.phase1a.attempt": ledger["attempt_id"],
            "clinic.phase1a.claim": claim["claim_id"],
            "clinic.phase1a.runner-create-intent": creation["intent_sha256"],
            "clinic.phase1a.service": creation["service_name"],
        },
        "mounts": [],
        "name": creation["container_name"],
        "network_mode": service["network_mode"],
        "open_stdin": True,
        "pid": 0 if state == "created" else 501,
        "ports": {},
        "read_only": False,
        "restart_count": 0,
        "shm_size": 67108864,
        "state": state,
        "tmpfs": {},
        "user": f"{service['uid']}:{service['gid']}",
    }
