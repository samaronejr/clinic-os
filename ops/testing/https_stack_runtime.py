"""Claimed Docker lifecycle for the production HTTPS acceptance stack."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Never

from ops.testing.https_probes import run_https_probes
from ops.testing.isolation_claim_transitions import reserve_claim
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    ensure_private_directory,
    load_json,
)
from ops.testing.isolation_docker_metadata import run_docker_command
from ops.testing.isolation_reconcile import reconcile_same_boot
from ops.testing.isolation_refresh import verify_claim
from ops.testing.isolation_runner_create import runner_tmpfs_options
from ops.testing.process_helpers import run_process
from ops.testing.runtime_paths import runtime_directory
from ops.testing.tls_contract import WEB_HOST

if TYPE_CHECKING:
    from collections.abc import Callable

    from ops.testing.https_stack_specs import HttpsStackPlan

READINESS_BOUND_SECONDS = 5
SIGTERM_BOUND_SECONDS = 35


def run_https_stack(
    repository: Path,
    plan: HttpsStackPlan,
    ca_path: Path,
    browser_probe: Callable[[Path], None] | None = None,
) -> None:
    """Reserve, activate, probe, and reverse-clean one isolated HTTPS stack."""
    ledger_path = (repository / ".omo/evidence/isolation-ledger-phase1a.json").resolve(
        strict=True
    )
    _reserve(ledger_path, plan.spec)
    container_ids: list[str] = []
    try:
        _create_owned_resources(plan)
        container_ids.extend(
            _create_service(plan, service) for service in _services(plan.spec)
        )
        containers = _container_map(plan, container_ids)
        for name in ("cleartext", "release", "web"):
            run_docker_command(("start", containers[name]))
        _prove_readiness_outage(plan, ca_path)
        run_docker_command(("start", containers["database"]))
        _wait_for_database(containers["database"])
        reconcile_same_boot(ledger_path)
        verify_claim(ledger_path, _text(plan.spec["claim_id"]), refresh=True)
        run_https_probes(
            repository, plan, containers, ca_path, browser_probe=browser_probe
        )
        _prove_sigterm(containers["web"])
    finally:
        for container_id in reversed(container_ids):
            _remove_container(container_id)
        _remove_network(plan.network_name)
        _remove_volume(plan.pgdata_name)
        reconcile_same_boot(ledger_path)


def _reserve(ledger_path: Path, spec: JsonObject) -> None:
    with runtime_directory(_run_root(ledger_path), purpose="https") as work:
        path = work / "https-stack-spec.json"
        path.write_bytes(canonical_bytes(spec))
        path.chmod(0o400)
        reserve_claim(ledger_path, path)


def _run_root(ledger_path: Path) -> Path:
    """Provision this caller's private run root under the attempt root."""
    ledger, _ = load_json(ledger_path)
    root = Path(_text(ledger.get("attempt_root"))) / "runtime"
    ensure_private_directory(root)
    return root


def _create_owned_resources(plan: HttpsStackPlan) -> None:
    claim_id = _text(plan.spec["claim_id"])
    label = f"clinic.phase1a.claim={claim_id}"
    run_docker_command(("volume", "create", "--label", label, plan.pgdata_name))
    run_docker_command(
        (
            "network",
            "create",
            "--driver",
            "bridge",
            "--label",
            label,
            plan.network_name,
        )
    )


def _create_service(plan: HttpsStackPlan, service: JsonObject) -> str:
    desired = _object(plan.spec["desired"])
    project = _text(desired["project"])
    name = _text(service["name"])
    arguments = [
        "create",
        "--name",
        f"{project}-{name}-1",
        "--label",
        f"clinic.phase1a.claim={_text(plan.spec['claim_id'])}",
        "--label",
        f"com.docker.compose.project={project}",
        "--label",
        f"com.docker.compose.service={name}",
    ]
    uid = service["uid"]
    gid = service["gid"]
    if uid != 0 or gid != 0:
        arguments.extend(("--user", f"{uid}:{gid}"))
    for key, value in sorted(plan.environments[name].items()):
        arguments.extend(("--env", f"{key}={value}"))
    for mount in _objects(service["volume_mounts"]):
        value = (
            f"type=volume,src={_text(mount['volume_name'])},"
            f"dst={_text(mount['target'])}"
        )
        if mount["read_only"] is True:
            value += ",readonly"
        arguments.extend(("--mount", value))
    for port in _objects(service["published_ports"]):
        arguments.extend(
            (
                "--publish",
                f"{_text(port['host'])}:{port['port']}:{port['container_port']}/tcp",
            )
        )
    _append_filesystem(arguments, service)
    arguments.extend(("--network", plan.network_name))
    for reference in _objects(service["network_refs"]):
        for alias in _strings(reference["aliases"]):
            arguments.extend(("--network-alias", alias))
    arguments.append(_text(service["image_id"]))
    arguments.extend(_strings(service["command"]))
    return run_docker_command(tuple(arguments)).strip()


def _wait_for_database(container_id: str) -> None:
    run_docker_command(
        (
            "exec",
            "--user",
            "999:999",
            container_id,
            "bash",
            "-ceu",
            "for n in $(seq 1 100); do pg_isready -U postgres && exit 0; "
            "sleep .2; done; exit 1",
        )
    )


def _prove_readiness_outage(plan: HttpsStackPlan, ca_path: Path) -> None:
    started = time.monotonic()
    status = ""
    while time.monotonic() - started <= READINESS_BOUND_SECONDS:
        result = run_process(
            (
                "/usr/bin/curl",
                "--silent",
                "--show-error",
                "--max-time",
                "4",
                "--cacert",
                str(ca_path),
                "--resolve",
                f"{WEB_HOST}:{plan.https_port}:127.0.0.1",
                "--output",
                "/dev/null",
                "--write-out",
                "%{http_code}",
                f"https://{WEB_HOST}:{plan.https_port}/readyz",
            ),
            timeout_seconds=5,
        )
        status = result.stdout
        if result.returncode == 0 and status == "503":
            elapsed = time.monotonic() - started
            sys.stdout.write(f"readiness-down-seconds={elapsed:.3f}\n")
            return
    _fail("readiness outage exceeded bound")


def _prove_sigterm(container_id: str) -> None:
    started = time.monotonic()
    run_docker_command(("stop", "--signal", "TERM", "--time", "25", container_id))
    elapsed = time.monotonic() - started
    exit_code = run_docker_command(
        ("inspect", "--format", "{{.State.ExitCode}}", container_id)
    ).strip()
    if elapsed > SIGTERM_BOUND_SECONDS or exit_code != "0":
        _fail("SIGTERM exceeded graceful bound")
    sys.stdout.write(f"sigterm-seconds={elapsed:.3f}\n")


def _append_filesystem(arguments: list[str], service: JsonObject) -> None:
    value = service["filesystem_contract"]
    if value is None:
        return
    contract = _object(value)
    arguments.append("--read-only")
    for mount in _objects(contract["tmpfs_mounts"]):
        arguments.extend(
            ("--tmpfs", f"{_text(mount['target'])}:{runner_tmpfs_options(mount)}")
        )
    arguments.extend(("--ipc", _text(contract["ipc_mode"])))
    arguments.extend(("--shm-size", str(contract["shm_size_bytes"])))


def _container_map(plan: HttpsStackPlan, ids: list[str]) -> dict[str, str]:
    return {
        _text(service["name"]): container_id
        for service, container_id in zip(_services(plan.spec), ids, strict=True)
    }


def _services(spec: JsonObject) -> list[JsonObject]:
    return _objects(_object(spec["desired"])["services"])


def _remove_container(container_id: str) -> None:
    listed = run_docker_command(
        (
            "container",
            "ls",
            "-aq",
            "--no-trunc",
            "--filter",
            f"id={container_id}",
        )
    )
    if listed.strip() == container_id:
        running = run_docker_command(
            ("inspect", "--format", "{{.State.Running}}", container_id)
        ).strip()
        if running == "true":
            run_docker_command(
                ("stop", "--signal", "TERM", "--time", "25", container_id)
            )
        run_docker_command(("rm", container_id))


def _remove_network(name: str) -> None:
    listed = run_docker_command(("network", "ls", "-q", "--filter", f"name=^{name}$"))
    if listed.strip():
        run_docker_command(("network", "rm", name))


def _remove_volume(name: str) -> None:
    listed = run_docker_command(("volume", "ls", "-q", "--filter", f"name=^{name}$"))
    if listed.strip() == name:
        run_docker_command(("volume", "rm", name))


def _object(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        _fail("HTTPS stack object is invalid")
    return value


def _objects(value: JsonValue) -> list[JsonObject]:
    if not isinstance(value, list):
        _fail("HTTPS stack array is invalid")
    result: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            _fail("HTTPS stack array is invalid")
        result.append(item)
    return result


def _strings(value: JsonValue) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _fail("HTTPS stack string array is invalid")
    return [item for item in value if isinstance(item, str)]


def _text(value: JsonValue) -> str:
    if not isinstance(value, str) or not value:
        _fail("HTTPS stack text is invalid")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
