"""Ledger-owned runtime probe for the clean candidate browser image."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never
from uuid import uuid4

from ops.testing.browser_runner_contract import filesystem_contract
from ops.testing.isolation_claim_transitions import reserve_claim
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
)
from ops.testing.isolation_docker_metadata import run_docker_command
from ops.testing.isolation_reconcile import reconcile_same_boot
from ops.testing.isolation_refresh import verify_claim
from ops.testing.isolation_runner_create import runner_tmpfs_options

if TYPE_CHECKING:
    from collections.abc import Sequence

RUNNER_ID: Final = 10001


def probe_runner_image(
    repository: Path,
    image_id: str,
    image_contract: JsonObject,
) -> None:
    """Reserve before proving nonroot identity and causal write confinement."""
    ledger_path = (repository / ".omo/evidence/isolation-ledger-phase1a.json").resolve(
        strict=True
    )
    claim_id = str(uuid4())
    project = f"clinic_runner_probe_{claim_id.split('-', 1)[0]}"
    service = _service(image_id, image_contract)
    desired: JsonObject = {
        "borrowed_network_refs": [],
        "borrowed_volume_refs": [],
        "database_names": [],
        "loopback_ports": [],
        "owned_networks": [],
        "owned_volumes": [],
        "project": project,
        "services": [service],
    }
    spec: JsonObject = {
        "claim_id": claim_id,
        "dependency_claim_ids": [],
        "desired": desired,
        "kind": "stack",
        "purpose": "browser-runner-image-probe",
    }
    with tempfile.TemporaryDirectory(dir="/tmp/opencode") as temporary:
        path = Path(temporary) / "spec.json"
        path.write_bytes(canonical_bytes(spec))
        path.chmod(0o400)
        reserve_claim(ledger_path, path)
    container_id = ""
    try:
        container_id = _create(project, claim_id, service)
        run_docker_command(("start", container_id))
        reconcile_same_boot(ledger_path)
        verify_claim(ledger_path, claim_id, refresh=True)
        attestation = run_docker_command(
            (
                "exec",
                container_id,
                "python",
                "-m",
                "ops.testing.browser_session",
                "attest",
            )
        )
        value: JsonValue = json.loads(attestation)
        if (
            not isinstance(value, dict)
            or value.get("uid") != RUNNER_ID
            or value.get("gid") != RUNNER_ID
            or value.get("architecture") != "x86_64"
            or value.get("manifest_sha256")
            != image_contract.get("source_manifest_sha256")
            or value.get("manifest_entry_count")
            != image_contract.get("source_entry_count")
        ):
            _fail("runner runtime attestation failed")
        _inspect_confinement(container_id, image_id)
        sys.stdout.write(attestation)
    finally:
        if container_id:
            running = run_docker_command(
                ("inspect", "--format", "{{.State.Running}}", container_id)
            ).strip()
            if running == "true":
                run_docker_command(
                    ("stop", "--signal", "TERM", "--time", "5", container_id)
                )
            run_docker_command(("rm", container_id))
        reconcile_same_boot(ledger_path)


def _service(image_id: str, image_contract: JsonObject) -> JsonObject:
    return {
        "command": ["hold"],
        "environment_contract": {
            "absent_keys": ["FORWARDED_ALLOW_IPS", "GUNICORN_CMD_ARGS"],
            "literal": [{"name": "PYTHONTZPATH", "value": ""}],
            "secret_keys": [],
        },
        "extra_hosts": [],
        "filesystem_contract": filesystem_contract(),
        "gid": 10001,
        "image_contract": image_contract,
        "image_id": image_id,
        "name": "browser",
        "network_mode": "none",
        "network_refs": [],
        "published_ports": [],
        "start_policy": "running-before-activation",
        "uid": 10001,
        "volume_mounts": [],
    }


def _create(
    project: str,
    claim_id: str,
    service: JsonObject,
) -> str:
    contract = _object(service["filesystem_contract"])
    arguments = [
        "create",
        "--name",
        f"{project}-browser-1",
        "--network",
        "none",
        "--user",
        "10001:10001",
        "--read-only",
        "--ipc",
        "none",
        "--shm-size",
        "0",
        "--label",
        f"clinic.phase1a.claim={claim_id}",
        "--label",
        f"com.docker.compose.project={project}",
        "--label",
        "com.docker.compose.service=browser",
    ]
    for mount in _objects(contract["tmpfs_mounts"]):
        arguments.extend(
            ("--tmpfs", f"{mount['target']}:{runner_tmpfs_options(mount)}")
        )
    arguments.extend((_text(service["image_id"]), "hold"))
    return run_docker_command(tuple(arguments)).strip()


def _inspect_confinement(container_id: str, image_id: str) -> None:
    fields = run_docker_command(
        (
            "inspect",
            "--format",
            "{{.Image}}|{{.Config.User}}|{{.HostConfig.ReadonlyRootfs}}|"
            "{{.HostConfig.IpcMode}}|{{len .HostConfig.Tmpfs}}",
            container_id,
        )
    ).strip()
    if fields != f"{image_id}|10001:10001|true|none|1":
        _fail("runner runtime confinement drifted")


def _object(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        _fail("runner probe object is invalid")
    return value


def _objects(value: JsonValue) -> Sequence[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail("runner probe array is invalid")
    return [item for item in value if isinstance(item, dict)]


def _text(value: JsonValue) -> str:
    if not isinstance(value, str) or not value:
        _fail("runner probe text is invalid")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
