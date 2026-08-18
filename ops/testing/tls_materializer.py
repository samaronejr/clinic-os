"""Ledger-owned lifecycle for the isolated six-volume TLS materializer."""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never
from uuid import uuid4

from ops.testing.isolation_claim_transitions import reserve_claim
from ops.testing.isolation_common import JsonObject, JsonValue, canonical_bytes
from ops.testing.isolation_docker_metadata import run_docker_command
from ops.testing.isolation_reconcile import reconcile_same_boot
from ops.testing.isolation_refresh import verify_claim
from ops.testing.tls_contract import POSTGRES_IMAGE
from ops.testing.tls_specs import (
    materializer_spec,
    materializer_volume_names,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

PULL_TIMEOUT_SECONDS: Final = 600
MATERIALIZATION_WAIT = (
    "for n in $(seq 1 100); do "
    "test -f /clinic-pki/.materialized && exit 0; "
    "sleep .2; done; exit 1"
)


@dataclass(frozen=True, slots=True)
class MaterializerLease:
    """Identity-bound active materializer resources owned by one stack claim."""

    claim_id: str
    container_id: str
    image_id: str
    ledger_path: Path
    project: str
    volume_names: dict[str, str]


@contextmanager
def materializer_lease(repository: Path) -> Iterator[MaterializerLease]:
    """Reserve before create and reverse-release every exact owned resource."""
    ledger_path = (repository / ".omo/evidence/isolation-ledger-phase1a.json").resolve(
        strict=True
    )
    claim_id = str(uuid4())
    project = f"clinic_phase1a_tls_{claim_id.split('-', 1)[0]}"
    image_id = _postgres_image_id()
    spec = materializer_spec(claim_id, project, image_id)
    desired = _object(spec.get("desired"))
    volumes = _objects(desired.get("owned_volumes"))
    service = _objects(desired.get("services"))[0]
    container_id = ""
    with tempfile.TemporaryDirectory(dir="/tmp/opencode") as temporary:
        spec_path = Path(temporary) / "materializer-spec.json"
        spec_path.write_bytes(canonical_bytes(spec))
        spec_path.chmod(0o400)
        reserve_claim(ledger_path, spec_path)
    try:
        for volume in volumes:
            run_docker_command(
                (
                    "volume",
                    "create",
                    "--label",
                    f"clinic.phase1a.claim={claim_id}",
                    _text(volume.get("volume_name")),
                )
            )
        container_id = _create_materializer(project, claim_id, service)
        run_docker_command(("start", container_id))
        run_docker_command(
            (
                "exec",
                container_id,
                "bash",
                "-ceu",
                MATERIALIZATION_WAIT,
            )
        )
        reconcile_same_boot(ledger_path)
        verify_claim(ledger_path, claim_id, refresh=True)
        yield MaterializerLease(
            claim_id,
            container_id,
            image_id,
            ledger_path,
            project,
            materializer_volume_names(project, claim_id),
        )
    finally:
        if container_id:
            running = run_docker_command(
                ("inspect", "--format", "{{.State.Running}}", container_id)
            ).strip()
            if running == "true":
                run_docker_command(
                    ("stop", "--signal", "TERM", "--time", "25", container_id)
                )
            run_docker_command(("rm", container_id))
        for volume in reversed(volumes):
            name = _text(volume.get("volume_name"))
            _remove_volume_if_present(name)
        reconcile_same_boot(ledger_path)


def _create_materializer(project: str, claim_id: str, service: JsonObject) -> str:
    arguments = [
        "create",
        "--name",
        f"{project}-materializer-1",
        "--network",
        "none",
        "--label",
        f"clinic.phase1a.claim={claim_id}",
        "--label",
        f"com.docker.compose.project={project}",
        "--label",
        "com.docker.compose.service=materializer",
    ]
    for mount in _objects(service.get("volume_mounts")):
        arguments.extend(
            (
                "--mount",
                "type=volume,src="
                f"{_text(mount.get('volume_name'))},dst={_text(mount.get('target'))}",
            )
        )
    arguments.append(POSTGRES_IMAGE)
    arguments.extend(_strings(service.get("command")))
    return run_docker_command(tuple(arguments)).strip()


def _postgres_image_id() -> str:
    if asyncio.run(_pull_postgres()) != 0:
        _fail("pinned PostgreSQL image is unavailable")
    return run_docker_command(
        ("image", "inspect", "--format", "{{.Id}}", POSTGRES_IMAGE)
    ).strip()


async def _pull_postgres() -> int:
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/docker",
        "pull",
        "--platform=linux/amd64",
        POSTGRES_IMAGE,
    )
    try:
        return await asyncio.wait_for(
            process.wait(),
            timeout=PULL_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        _fail("pinned PostgreSQL image pull timed out")


def _remove_volume_if_present(name: str) -> None:
    listed = run_docker_command(
        ("volume", "ls", "--quiet", "--filter", f"name=^{name}$")
    )
    if listed.strip() == name:
        run_docker_command(("volume", "rm", name))


def _object(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        _fail("materializer object is invalid")
    return value


def _objects(value: JsonValue) -> list[JsonObject]:
    if not isinstance(value, list):
        _fail("materializer array is invalid")
    result: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            _fail("materializer array is invalid")
        result.append(item)
    return result


def _strings(value: JsonValue) -> list[str]:
    if not isinstance(value, list):
        _fail("materializer strings are invalid")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            _fail("materializer strings are invalid")
        result.append(item)
    return result


def _text(value: JsonValue) -> str:
    if not isinstance(value, str) or not value:
        _fail("materializer text is invalid")
    return value


def _fail(message: str) -> Never:
    raise _MaterializerError(message)


class _MaterializerError(RuntimeError):
    pass
