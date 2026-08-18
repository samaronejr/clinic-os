"""Construct and destroy one claimed hosted-CI PostgreSQL stack."""

from __future__ import annotations

import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Never

from ops.testing.isolation_claim_transitions import reserve_claim
from ops.testing.isolation_common import JsonObject, JsonValue, canonical_bytes
from ops.testing.isolation_docker_metadata import run_docker_command

POSTGRES_PORT = 5432


@dataclass(frozen=True, slots=True)
class _CiDatabaseSpec:
    claim_id: str
    materializer_claim: str
    ca_claim: str
    project: str
    network: str
    pgdata: str
    database: str
    port: int
    tls_volume: str
    service: JsonObject


def _spec(spec: _CiDatabaseSpec) -> JsonObject:
    labels: list[JsonValue] = [{"name": "clinic.phase1a.claim", "value": spec.claim_id}]
    desired: JsonObject = {
        "borrowed_network_refs": [],
        "borrowed_volume_refs": [
            {
                "access": "read-only",
                "owner_claim_id": spec.materializer_claim,
                "volume_name": spec.tls_volume,
            }
        ],
        "database_names": [spec.database],
        "loopback_ports": [
            {"host": "127.0.0.1", "port": spec.port, "transport": "tcp"}
        ],
        "owned_networks": [
            {
                "attachable": False,
                "driver": "bridge",
                "internal": False,
                "labels": labels,
                "network_name": spec.network,
            }
        ],
        "owned_volumes": [
            {"driver": "local", "labels": labels, "volume_name": spec.pgdata}
        ],
        "project": spec.project,
        "services": [spec.service],
    }
    dependencies: list[JsonValue] = []
    dependencies.extend(sorted([spec.ca_claim, spec.materializer_claim]))
    return {
        "claim_id": spec.claim_id,
        "dependency_claim_ids": dependencies,
        "desired": desired,
        "kind": "stack",
        "purpose": "hosted-ci-postgres",
    }


def _reserve(ledger: Path, spec: JsonObject) -> None:
    with tempfile.TemporaryDirectory(dir="/tmp/opencode") as temporary:
        path = Path(temporary) / "ci-postgres-spec.json"
        path.write_bytes(canonical_bytes(spec))
        path.chmod(0o400)
        reserve_claim(ledger, path)


def _create_container(
    project: str,
    claim_id: str,
    service: JsonObject,
    environment: dict[str, str],
) -> str:
    arguments = [
        "create",
        "--name",
        f"{project}-database-1",
        "--label",
        f"clinic.phase1a.claim={claim_id}",
        "--label",
        f"com.docker.compose.project={project}",
        "--label",
        "com.docker.compose.service=database",
    ]
    for key, value in sorted(environment.items()):
        arguments.extend(("--env", f"{key}={value}"))
    for mount in _objects(service["volume_mounts"]):
        suffix = ",readonly" if mount["read_only"] is True else ""
        arguments.extend(
            (
                "--mount",
                f"type=volume,src={mount['volume_name']},dst={mount['target']}{suffix}",
            )
        )
    port = _objects(service["published_ports"])[0]
    arguments.extend(("--publish", f"127.0.0.1:{port['port']}:5432/tcp"))
    network = _objects(service["network_refs"])[0]
    aliases = _strings(network["aliases"])
    if len(aliases) != 1:
        _fail("CI PostgreSQL network alias is invalid")
    arguments.extend(
        (
            "--network",
            str(network["network_name"]),
            "--network-alias",
            aliases[0],
        )
    )
    arguments.append(str(service["image_id"]))
    arguments.extend(_strings(service["command"]))
    return run_docker_command(tuple(arguments)).strip()


def _wait(container_id: str) -> None:
    run_docker_command(
        (
            "exec",
            container_id,
            "bash",
            "-ceu",
            "for n in $(seq 1 150); do pg_isready -h 127.0.0.1 -U postgres && exit 0; "
            "sleep .2; done; exit 1",
        )
    )


def _remove_container(container_id: str) -> None:
    running = run_docker_command(
        ("inspect", "--format", "{{.State.Running}}", container_id)
    ).strip()
    if running == "true":
        run_docker_command(("stop", "--signal", "TERM", "--time", "25", container_id))
    run_docker_command(("rm", container_id))


def _remove(kind: str, name: str) -> None:
    listed = run_docker_command((kind, "ls", "-q", "--filter", f"name=^{name}$"))
    if listed.strip():
        run_docker_command((kind, "rm", name))


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
    if port == POSTGRES_PORT:
        return _unused_port()
    return port


def _objects(value: JsonValue) -> list[JsonObject]:
    if not isinstance(value, list):
        _fail("CI PostgreSQL object array is invalid")
    result: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            _fail("CI PostgreSQL object array is invalid")
        result.append(item)
    return result


def _strings(value: JsonValue) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _fail("CI PostgreSQL string array is invalid")
    return [item for item in value if isinstance(item, str)]


def _fail(message: str) -> Never:
    raise RuntimeError(message)
