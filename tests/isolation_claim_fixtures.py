from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Final, Protocol, cast

from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    JsonObject,
    JsonValue,
    canonical_bytes,
    raw_sha256,
    write_no_replace,
)
from ops.testing.isolation_snapshot import SnapshotRequest, snapshot_ledger
from ops.testing.process_helpers import run_process

from isolation_probe_fixtures import complete_probe

CLAIM_ID: Final = "12345678-1234-4123-8123-123456789abc"
MISSING_CLAIM_ID: Final = "22345678-1234-4123-8123-123456789abc"
SECOND_CLAIM_ID: Final = "32345678-1234-4123-8123-123456789abc"
FOUNDATION_SHA: Final = "a" * 40
OUTPUT_BYTES: Final = b"{}\n"
STACK_PORT: Final = 15432


class ClaimTransitions(Protocol):
    def reserve_claim(self, ledger_path: Path, spec_path: Path) -> str: ...

    def activate_claim(
        self,
        ledger_path: Path,
        claim_id: str,
        observed_path: Path,
    ) -> None: ...

    def release_claim(self, ledger_path: Path, claim_id: str) -> None: ...


def claim_transitions() -> ClaimTransitions:
    return cast(
        "ClaimTransitions",
        importlib.import_module("ops.testing.isolation_claim_transitions"),
    )


def snapshot(tmp_path: Path) -> Path:
    authority_workspace = tmp_path / "authority"
    authority_root = authority_workspace / ".omo"
    worktree = tmp_path / "feature"
    authority_root.mkdir(parents=True)
    worktree.mkdir()
    plan = tmp_path / "approved.md"
    plan.write_bytes(b"# approved\n")
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    proof_path = authority_root / f"clinic-os-phase1a-execution-host-{boot_id}.json"
    proof: JsonObject = {
        "boot_id": boot_id,
        "cgroup_parent_identity": {
            "device": 1,
            "gid": os.getegid(),
            "inode": 2,
            "link_count": 2,
            "mode": 0o755,
            "uid": os.geteuid(),
        },
        "cgroup_parent_path": "/sys/fs/cgroup/test",
        "foundation_sha": FOUNDATION_SHA,
        "workspace_realpath": str(authority_workspace),
    }
    write_no_replace(proof_path, canonical_bytes(proof), mode=MODE_IMMUTABLE)
    ledger_path = snapshot_ledger(
        SnapshotRequest(
            approved_plan=plan,
            authority_workspace=authority_workspace,
            authority_root=authority_root,
            foundation_sha=FOUNDATION_SHA,
            worktree=worktree,
            execution_host_preflight=proof_path,
            inventory_fixture={
                "containers": [],
                "listeners": [],
                "networks": [],
                "volumes": [],
            },
        ),
        probe_runner=complete_probe,
    )
    for arguments in (
        ("/usr/bin/git", "-C", str(worktree), "init", "-q"),
        ("/usr/bin/git", "-C", str(worktree), "add", "-A"),
        (
            "/usr/bin/git",
            "-C",
            str(worktree),
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=test",
            "commit",
            "-qm",
            "init",
        ),
    ):
        assert run_process(arguments).returncode == 0
    return ledger_path


def filesystem_spec(claim_id: str, dependencies: list[JsonValue]) -> JsonObject:
    return {
        "claim_id": claim_id,
        "dependency_claim_ids": dependencies,
        "desired": {
            "owned_files": [
                {
                    "gid": os.getegid(),
                    "mode": 0o600,
                    "relative_path": "staging/output.json",
                    "sha256": raw_sha256(OUTPUT_BYTES),
                    "uid": os.geteuid(),
                }
            ],
            "published_outputs": [],
        },
        "kind": "filesystem",
        "purpose": "todo-evidence-staging",
    }


def write_spec(tmp_path: Path, spec: JsonObject) -> Path:
    return write_immutable_json(tmp_path / f"{spec['claim_id']}.json", spec)


def write_immutable_json(path: Path, value: JsonObject) -> Path:
    write_no_replace(path, canonical_bytes(value), mode=MODE_IMMUTABLE)
    return path


def stack_spec(claim_id: str, project: str, port: int) -> JsonObject:
    return {
        "claim_id": claim_id,
        "dependency_claim_ids": [],
        "desired": {
            "borrowed_network_refs": [],
            "borrowed_volume_refs": [],
            "database_names": [project],
            "loopback_ports": [{"host": "127.0.0.1", "port": port, "transport": "tcp"}],
            "owned_networks": [],
            "owned_volumes": [],
            "project": project,
            "services": [
                {
                    "command": ["postgres"],
                    "environment_contract": {
                        "absent_keys": [],
                        "literal": [],
                        "secret_keys": ["POSTGRES_PASSWORD"],
                    },
                    "extra_hosts": [],
                    "filesystem_contract": None,
                    "gid": 999,
                    "image_contract": None,
                    "image_id": "sha256:" + ("d" * 64),
                    "name": "db",
                    "network_mode": "none",
                    "network_refs": [],
                    "published_ports": [
                        {
                            "container_port": 5432,
                            "host": "127.0.0.1",
                            "port": port,
                            "transport": "tcp",
                        }
                    ],
                    "start_policy": "running-before-activation",
                    "uid": 999,
                    "volume_mounts": [],
                }
            ],
        },
        "kind": "stack",
        "purpose": "isolated-postgresql",
    }


def stack_observation(spec: JsonObject) -> JsonObject:
    desired = spec["desired"]
    assert isinstance(desired, dict)
    services = desired["services"]
    assert isinstance(services, list)
    service = services[0]
    assert isinstance(service, dict)
    published_ports = service["published_ports"]
    assert isinstance(published_ports, list)
    container_id = "e" * 64
    listeners: list[JsonObject] = []
    for published_port in published_ports:
        assert isinstance(published_port, dict)
        listeners.append(
            {
                "argv_sha256": None,
                "container_id": container_id,
                "executable_realpath": None,
                "host": published_port["host"],
                "owner_kind": "container",
                "pid": None,
                "port": published_port["port"],
                "process_start_ticks": None,
                "socket_inode": 123,
                "transport": published_port["transport"],
            }
        )
    return {
        "borrowed_networks": [],
        "borrowed_volumes": [],
        "container_ids": [container_id],
        "listeners": cast("JsonValue", listeners),
        "owned_networks": [],
        "owned_volumes": [],
        "services": [
            {
                "command": service["command"],
                "container_id": container_id,
                "environment_contract": service["environment_contract"],
                "extra_hosts": service["extra_hosts"],
                "filesystem_contract": service["filesystem_contract"],
                "gid": service["gid"],
                "image_contract": service["image_contract"],
                "image_id": service["image_id"],
                "name": service["name"],
                "network_attachments": [],
                "network_mode": service["network_mode"],
                "published_ports": published_ports,
                "state": "running",
                "uid": service["uid"],
                "volume_mounts": service["volume_mounts"],
            }
        ],
    }


def runner_stack_spec(claim_id: str) -> JsonObject:
    spec = stack_spec(claim_id, "clinic_phase1a_runner", STACK_PORT)
    desired = spec["desired"]
    assert isinstance(desired, dict)
    desired["loopback_ports"] = []
    services = desired["services"]
    assert isinstance(services, list)
    service = services[0]
    assert isinstance(service, dict)
    service["published_ports"] = []
    service["start_policy"] = "prepared-attest-before-use"
    spec["purpose"] = "host-http"
    return spec
