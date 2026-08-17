from __future__ import annotations

import hashlib
import os
import stat
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final

from ops.testing.isolation_stack_claim import validate_stack_desired
from ops.testing.isolation_stack_observation import validate_stack_observation

from .browser_authority_root import (
    integer,
    object_array,
    object_value,
    parse_timestamp,
    require_fresh,
    string_array,
    text,
)
from .browser_process_contract import validate_browser_process

if TYPE_CHECKING:
    from pathlib import Path

    from ops.testing.isolation_common import JsonObject

CLAIM_KEYS: Final = frozenset(
    {
        "activated_at_utc",
        "candidate_envelope_binding",
        "claim_id",
        "dependency_claim_ids",
        "desired",
        "kind",
        "last_verified_at_utc",
        "observed",
        "prepared_at_utc",
        "purpose",
        "reserved_at_utc",
        "root_relative_path",
        "runner_creation",
        "status",
    }
)
PRIVATE_DIRECTORY_MODE: Final = 0o700


def validate_browser_claims(
    ledger: JsonObject,
    expected: JsonObject,
    phase: str,
    attempt_root: Path,
) -> JsonObject:
    claims = object_array(ledger.get("claims"))
    expected_ids = {
        text(expected.get("materializer_claim_id")),
        text(expected.get("database_claim_id")),
        text(expected.get("ca_export_claim_id")),
        text(expected.get("process_claim_id")),
    }
    by_id = {text(claim.get("claim_id")): claim for claim in claims}
    if set(by_id) != expected_ids or len(by_id) != len(claims):
        raise ValueError
    materializer = by_id[text(expected.get("materializer_claim_id"))]
    database = by_id[text(expected.get("database_claim_id"))]
    ca_export = by_id[text(expected.get("ca_export_claim_id"))]
    process = by_id[text(expected.get("process_claim_id"))]
    _header(materializer, "stack", "tls-materializer", "active")
    _header(database, "stack", "browser-database", "active")
    _header(ca_export, "filesystem", "browser-ca-export", "active")
    _header(process, "process", "browser-server", phase)
    materializer_id = text(materializer.get("claim_id"))
    database_id = text(database.get("claim_id"))
    ca_id = text(ca_export.get("claim_id"))
    if (
        string_array(database.get("dependency_claim_ids")) != [materializer_id]
        or string_array(ca_export.get("dependency_claim_ids")) != [materializer_id]
        or string_array(process.get("dependency_claim_ids"))
        != [materializer_id, database_id, ca_id]
    ):
        raise ValueError
    _stack(materializer)
    _database_stack(database, expected)
    ca_file = _ca_export(ca_export, attempt_root)
    validate_browser_process(process, expected, phase, ca_file)
    _baseline_collisions(ledger, materializer, database, process)
    return process


def _header(claim: JsonObject, kind: str, purpose: str, status: str) -> None:
    claim_id = text(claim.get("claim_id"))
    if (
        set(claim) != CLAIM_KEYS
        or claim.get("kind") != kind
        or claim.get("purpose") != purpose
        or claim.get("status") != status
        or claim.get("root_relative_path") != f"claims/{claim_id}"
        or claim.get("runner_creation") is not None
        or claim.get("candidate_envelope_binding") is not None
    ):
        raise ValueError
    require_fresh(claim.get("last_verified_at_utc"))
    parse_timestamp(claim.get("reserved_at_utc"))
    if status == "reserved":
        if claim.get("activated_at_utc") is not None:
            raise ValueError
    else:
        parse_timestamp(claim.get("activated_at_utc"))


def _stack(claim: JsonObject) -> None:
    desired = object_value(claim.get("desired"))
    validate_stack_desired(desired)
    validate_stack_observation(object_value(claim.get("observed")), claim)


def _database_stack(claim: JsonObject, expected: JsonObject) -> None:
    _stack(claim)
    desired = object_value(claim.get("desired"))
    port = integer(expected.get("database_port"))
    endpoint = {"host": "127.0.0.1", "port": port, "transport": "tcp"}
    if (
        desired.get("project") != expected.get("project")
        or desired.get("database_names") != [expected.get("database_name")]
        or desired.get("loopback_ports") != [endpoint]
    ):
        raise ValueError


def _ca_export(claim: JsonObject, attempt_root: Path) -> JsonObject:
    desired = object_value(claim.get("desired"))
    observed = object_value(claim.get("observed"))
    if set(desired) != {"owned_files", "published_outputs"} or set(observed) != {
        "owned_files",
        "published_outputs",
    }:
        raise ValueError
    desired_files = object_array(desired.get("owned_files"))
    observed_files = object_array(observed.get("owned_files"))
    if len(desired_files) != 1 or len(observed_files) != 1:
        raise ValueError
    wanted = desired_files[0]
    recorded = observed_files[0]
    projection = {
        key: recorded.get(key)
        for key in ("relative_path", "mode", "uid", "gid", "sha256")
    }
    if (
        wanted != projection
        or desired.get("published_outputs") != []
        or observed.get("published_outputs") != []
    ):
        raise ValueError
    root = attempt_root / text(claim.get("root_relative_path"))
    relative = text(recorded.get("relative_path"))
    relative_path = PurePosixPath(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError
    if str(relative_path) != relative:
        raise ValueError
    path = root / relative
    root_identity = root.stat(follow_symlinks=False)
    identity = path.stat(follow_symlinks=False)
    actual: JsonObject = {
        "device": identity.st_dev,
        "gid": identity.st_gid,
        "inode": identity.st_ino,
        "mode": stat.S_IMODE(identity.st_mode),
        "relative_path": text(recorded.get("relative_path")),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "uid": identity.st_uid,
    }
    if (
        root.is_symlink()
        or stat.S_IMODE(root_identity.st_mode) != PRIVATE_DIRECTORY_MODE
        or root_identity.st_uid != os.geteuid()
        or root_identity.st_gid != os.getegid()
        or path.is_symlink()
        or not stat.S_ISREG(identity.st_mode)
        or actual != recorded
    ):
        raise ValueError
    return {"owner_claim_id": claim.get("claim_id"), **recorded}


def _baseline_collisions(
    ledger: JsonObject,
    materializer: JsonObject,
    database: JsonObject,
    process: JsonObject,
) -> None:
    baseline = object_value(ledger.get("baseline"))
    observed_stacks = [
        object_value(item.get("observed")) for item in (materializer, database)
    ]
    container_ids = {
        value for item in observed_stacks for value in _texts(item.get("container_ids"))
    }
    volume_names = {
        text(volume.get("volume_name"))
        for item in observed_stacks
        for volume in object_array(item.get("owned_volumes"))
    }
    network_ids = {
        text(network.get("network_id"))
        for item in observed_stacks
        for network in object_array(item.get("owned_networks"))
    }
    network_names = {
        text(network.get("network_name"))
        for item in observed_stacks
        for network in object_array(item.get("owned_networks"))
    }
    listener_keys = {
        (listener.get("host"), listener.get("port"))
        for item in (*observed_stacks, object_value(process.get("observed")))
        for listener in object_array(item.get("listeners"))
    }
    listener_keys.update(
        (listener.get("host"), listener.get("port"))
        for listener in object_array(
            object_value(process.get("desired")).get("host_ports")
        )
    )
    if any(
        item.get("id") in container_ids
        for item in object_array(baseline.get("containers"))
    ):
        raise ValueError
    if any(
        item.get("volume_name") in volume_names
        for item in object_array(baseline.get("volumes"))
    ):
        raise ValueError
    if any(
        item.get("id") in network_ids or item.get("name") in network_names
        for item in object_array(baseline.get("networks"))
    ):
        raise ValueError
    if any(
        (item.get("host"), item.get("port")) in listener_keys
        for item in object_array(baseline.get("listeners"))
    ):
        raise ValueError


def _texts(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError
    return [item for item in value if isinstance(item, str)]
