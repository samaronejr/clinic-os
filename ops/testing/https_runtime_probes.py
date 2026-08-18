"""Inspect runtime confinement and certificate ownership for HTTPS services."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Final, Never

from ops.testing.https_contract import TMPFS_TARGET
from ops.testing.isolation_common import IsolationError
from ops.testing.isolation_docker_metadata import run_docker_command

if TYPE_CHECKING:
    from ops.testing.https_stack_specs import HttpsStackPlan

APPLICATION_ID: Final = 10001


def inspect_https_runtime(
    plan: HttpsStackPlan,
    containers: dict[str, str],
) -> None:
    """Require exact nonroot confinement, commands, images, and TLS file modes."""
    desired = plan.spec["desired"]
    if not isinstance(desired, dict):
        _fail("HTTPS desired contract is invalid")
    services = desired["services"]
    if not isinstance(services, list):
        _fail("HTTPS services contract is invalid")
    by_name = {
        item["name"]: item
        for item in services
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    application_ids: set[str] = set()
    for name in ("cleartext", "release", "web"):
        container_id = containers[name]
        service = by_name[name]
        user = _inspect(container_id, "{{.Config.User}}")
        read_only = _inspect(container_id, "{{.HostConfig.ReadonlyRootfs}}")
        ipc = _inspect(container_id, "{{.HostConfig.IpcMode}}")
        tmpfs = _json(_inspect(container_id, "{{json .HostConfig.Tmpfs}}"))
        command = _json(_inspect(container_id, "{{json .Config.Cmd}}"))
        image_id = _inspect(container_id, "{{.Image}}")
        if (
            user != "10001:10001"
            or read_only != "true"
            or ipc != "none"
            or not isinstance(tmpfs, dict)
            or set(tmpfs) != {TMPFS_TARGET}
            or command != service.get("command")
            or image_id != service.get("image_id")
        ):
            _fail("HTTPS application runtime contract drifted")
        application_ids.add(image_id)
    if len(application_ids) != 1:
        _fail("HTTPS services substituted the application image")
    runtime_script = """
import errno
import hashlib
import importlib.metadata
import json
import os
import platform
import sys
import zoneinfo
from pathlib import Path

raw = Path("/app/application-source-manifest.json").read_bytes()
manifest = json.loads(raw)
release = Path("/etc/os-release").read_text()
shm_probe = Path("/dev/shm/clinic-write-probe")
try:
    shm_probe.write_text("synthetic", encoding="ascii")
except OSError as error:
    shm_write_denied = error.errno in {errno.ENOENT, errno.EACCES, errno.EROFS}
else:
    shm_probe.unlink(missing_ok=True)
    shm_write_denied = False
print(json.dumps({
    "architecture": platform.machine(),
    "debian_bookworm": "VERSION_CODENAME=bookworm" in release,
    "gid": os.getgid(),
    "manifest_entry_count": len(manifest["entries"]),
    "manifest_sha256": hashlib.sha256(raw).hexdigest(),
    "python": list(sys.version_info[:3]),
    "shm_write_denied": shm_write_denied,
    "static_manifest": Path("/app/staticfiles/staticfiles.json").is_file(),
    "tzdata": importlib.metadata.version("tzdata"),
    "tzpath": list(zoneinfo.TZPATH),
    "uid": os.getuid(),
}, sort_keys=True))
""".strip()
    runtime_raw = run_docker_command(
        ("exec", containers["web"], "python", "-c", runtime_script)
    )
    runtime = _json(runtime_raw)
    web_contract = by_name["web"].get("image_contract")
    if not isinstance(runtime, dict) or not isinstance(web_contract, dict):
        _fail("application runtime metadata is invalid")
    if (
        runtime.get("architecture") != "x86_64"
        or runtime.get("debian_bookworm") is not True
        or runtime.get("python") != [3, 13, 14]
        or runtime.get("shm_write_denied") is not True
        or runtime.get("static_manifest") is not True
        or runtime.get("uid") != APPLICATION_ID
        or runtime.get("gid") != APPLICATION_ID
        or runtime.get("tzdata") != "2026.3"
        or runtime.get("tzpath") != []
        or runtime.get("manifest_sha256") != web_contract.get("source_manifest_sha256")
        or runtime.get("manifest_entry_count") != web_contract.get("source_entry_count")
    ):
        _fail("application runtime metadata drifted")
    database_proof = run_docker_command(
        (
            "exec",
            "--user",
            "999:999",
            containers["database"],
            "stat",
            "-c",
            "%u:%g:%a:%n",
            "/run/clinic-test-db-tls/tls/tls.crt",
            "/run/clinic-test-db-tls/tls/tls.key",
            "/run/clinic-test-db-tls/tls/pg_hba.conf",
        )
    )
    web_proof = run_docker_command(
        (
            "exec",
            containers["web"],
            "stat",
            "-c",
            "%u:%g:%a:%n",
            "/run/clinic-test-tls/tls/tls.crt",
            "/run/clinic-test-tls/tls/tls.key",
            "/run/clinic-test-db-trust",
            "/run/clinic-test-db-trust/db-ca.pem",
        )
    )
    required = ("999:999:444", "999:999:400", "10001:10001:444")
    if any(value not in database_proof + web_proof for value in required):
        _fail("HTTPS certificate ownership contract drifted")
    sys.stdout.write(database_proof)
    sys.stdout.write(web_proof)


def _inspect(identifier: str, template: str) -> str:
    return run_docker_command(("inspect", "--format", template, identifier)).strip()


def _json(raw: str) -> object:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        message = "HTTPS inspect JSON is invalid"
        raise IsolationError(message) from error


def _fail(message: str) -> Never:
    raise IsolationError(message)
