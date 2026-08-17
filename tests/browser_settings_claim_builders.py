from __future__ import annotations

import copy
import os
from typing import TYPE_CHECKING

from browser_settings_claim_values import (
    CA_EXPORT_ID,
    DATABASE_ID,
    MATERIALIZER_ID,
    MATERIALIZER_NETWORK,
    PROCESS_ID,
    PROCESS_PORT,
    argv_sha,
    gunicorn_argv,
    lstat_record,
    object_array,
    object_value,
    sha,
    strings,
)
from isolation_claim_fixtures import stack_observation, stack_spec

if TYPE_CHECKING:
    from pathlib import Path

    from ops.testing.isolation_common import JsonObject, JsonValue


def active_process_ledger(ledger: JsonObject) -> JsonObject:
    result = copy.deepcopy(ledger)
    process = _claim(result, PROCESS_ID)
    desired = object_value(process["desired"])
    master = _member(5001, 4000, 5001, desired)
    workers: list[JsonValue] = [
        _member(5002, 5001, 5001, desired),
        _member(5003, 5001, 5001, desired),
    ]
    members: list[JsonValue] = [master, *workers]
    listener: JsonObject = {
        "argv_sha256": master["argv_sha256"],
        "container_id": None,
        "executable_realpath": master["executable_realpath"],
        "host": "127.0.0.1",
        "owner_kind": "process",
        "pid": 5001,
        "port": PROCESS_PORT,
        "process_start_ticks": master["start_ticks"],
        "socket_inode": 9001,
        "transport": "tcp",
    }
    process["activated_at_utc"] = process["last_verified_at_utc"]
    process["observed"] = {
        "listener_socket_inode": 9001,
        "listeners": [listener],
        "members": members,
    }
    process["status"] = "active"
    return result


def active_stack(claim_id: str, purpose: str, port: int, timestamp: str) -> JsonObject:
    spec = stack_spec(claim_id, f"clinic_phase1a_{claim_id[:8]}", port)
    claim = claim_record(spec, purpose, timestamp, "active")
    claim["observed"] = stack_observation(spec)
    return claim


def add_materializer_volume(claim: JsonObject, tmp_path: Path) -> None:
    desired = object_value(claim["desired"])
    service = _first(object_array(desired["services"]))
    labels: list[JsonValue] = [
        {"name": "clinic.phase1a.claim", "value": MATERIALIZER_ID}
    ]
    volume_name = "clinic_phase1a_materializer_fixture_public_trust"
    desired["owned_volumes"] = [
        {"driver": "local", "labels": labels, "volume_name": volume_name}
    ]
    service["volume_mounts"] = [
        {"read_only": False, "target": "/trust", "volume_name": volume_name}
    ]
    observed = object_value(claim["observed"])
    observed_service = _first(object_array(observed["services"]))
    observed_service["volume_mounts"] = service["volume_mounts"]
    observed["owned_volumes"] = [
        {
            "created_at": "2026-08-17T00:00:00Z",
            "driver": "local",
            "labels": labels,
            "mountpoint": str(tmp_path),
            "options": [],
            "scope": "local",
            "volume_name": volume_name,
        }
    ]
    desired["owned_networks"] = [
        {
            "attachable": False,
            "driver": "bridge",
            "internal": True,
            "labels": labels,
            "network_name": MATERIALIZER_NETWORK,
        }
    ]
    observed["owned_networks"] = [
        {
            "attachable": False,
            "driver": "bridge",
            "internal": True,
            "labels": labels,
            "network_id": "f" * 64,
            "network_name": MATERIALIZER_NETWORK,
        }
    ]


def active_filesystem(timestamp: str, observed: JsonObject) -> JsonObject:
    desired_file = {
        key: observed[key] for key in ("relative_path", "mode", "uid", "gid", "sha256")
    }
    return {
        "activated_at_utc": timestamp,
        "candidate_envelope_binding": None,
        "claim_id": CA_EXPORT_ID,
        "dependency_claim_ids": [MATERIALIZER_ID],
        "desired": {"owned_files": [desired_file], "published_outputs": []},
        "kind": "filesystem",
        "last_verified_at_utc": timestamp,
        "observed": {"owned_files": [observed], "published_outputs": []},
        "prepared_at_utc": None,
        "purpose": "browser-ca-export",
        "reserved_at_utc": timestamp,
        "root_relative_path": f"claims/{CA_EXPORT_ID}",
        "runner_creation": None,
        "status": "active",
    }


def reserved_process(
    timestamp: str,
    launcher: Path,
    config: Path,
    environment: JsonObject,
    ca_file: JsonObject,
) -> JsonObject:
    argv = gunicorn_argv(launcher, config)
    desired: JsonObject = {
        "argv": strings(argv),
        "argv_sha256": argv_sha(argv),
        "borrowed_file_refs": [
            {"access": "read-only", "owner_claim_id": CA_EXPORT_ID, **ca_file}
        ],
        "environment_contract": environment,
        "gid": os.getegid(),
        "gunicorn_config_lstat": lstat_record(config.lstat()),
        "gunicorn_config_path": str(config),
        "gunicorn_config_sha256": sha(config.read_bytes()),
        "host_ports": [{"host": "127.0.0.1", "port": PROCESS_PORT, "transport": "tcp"}],
        "interpreter_realpath": str(launcher.resolve(strict=True)),
        "interpreter_sha256": sha(launcher.resolve(strict=True).read_bytes()),
        "launcher_lstat": lstat_record(launcher.lstat(), launcher),
        "launcher_path": str(launcher),
        "module": "gunicorn",
        "process_model": "gunicorn-prefork",
        "uid": os.geteuid(),
        "worker_count": 2,
    }
    spec: JsonObject = {
        "claim_id": PROCESS_ID,
        "dependency_claim_ids": [MATERIALIZER_ID, DATABASE_ID, CA_EXPORT_ID],
        "desired": desired,
        "kind": "process",
        "purpose": "browser-server",
    }
    return claim_record(spec, "browser-server", timestamp, "reserved")


def claim_record(
    spec: JsonObject, purpose: str, timestamp: str, status: str
) -> JsonObject:
    return {
        "activated_at_utc": timestamp if status == "active" else None,
        "candidate_envelope_binding": None,
        "claim_id": spec["claim_id"],
        "dependency_claim_ids": spec["dependency_claim_ids"],
        "desired": spec["desired"],
        "kind": spec["kind"],
        "last_verified_at_utc": timestamp,
        "observed": (
            {"listener_socket_inode": None, "listeners": [], "members": []}
            if spec["kind"] == "process"
            else {}
        ),
        "prepared_at_utc": None,
        "purpose": purpose,
        "reserved_at_utc": timestamp,
        "root_relative_path": f"claims/{spec['claim_id']}",
        "runner_creation": None,
        "status": status,
    }


def _claim(ledger: JsonObject, claim_id: str) -> JsonObject:
    return next(
        item for item in object_array(ledger["claims"]) if item["claim_id"] == claim_id
    )


def _member(pid: int, ppid: int, group: int, desired: JsonObject) -> JsonObject:
    return {
        "argv_sha256": desired["argv_sha256"],
        "executable_realpath": desired["interpreter_realpath"],
        "gid": desired["gid"],
        "pgid": group,
        "pid": pid,
        "ppid": ppid,
        "sid": group,
        "start_ticks": pid * 10,
        "uid": desired["uid"],
    }


def _first(values: list[JsonObject]) -> JsonObject:
    assert len(values) == 1
    return values[0]
