from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import pytest
from ops.testing.browser_runner_contract import filesystem_contract, selected_suites
from ops.testing.browser_runtime_dispatch import (
    build_runtime_dispatch_frame,
    dispatch_runtime_frame,
)
from ops.testing.isolation_stack_service import validate_stack_service
from ops.testing.process_helpers import run_process

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLAYWRIGHT_IMAGE = (
    "mcr.microsoft.com/playwright/python:v1.61.0-noble@"
    "sha256:a9731514f24121d1dcd25d58d0a38146646d290a5998fd80d3e533e7b5e21c69"
)
UV_IMAGE = (
    "ghcr.io/astral-sh/uv:0.10.6@"
    "sha256:2f2ccd27bbf953ec7a9e3153a4563705e41c852a5e1912b438fc44d88d6cb52c"
)
APT_TRANSACTION = (
    "apt-get update && apt-get install -y --no-install-recommends "
    "libnss3-tools=2:3.98-1ubuntu0.2 && rm -rf /var/lib/apt/lists/*"
)


def test_browser_runner_dockerfile_has_one_pinned_apt_transaction() -> None:
    dockerfile = (PROJECT_ROOT / "ops/testing/browser-runner.Dockerfile").read_text()
    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]

    assert from_lines == [
        f"FROM --platform=linux/amd64 {UV_IMAGE} AS uv",
        f"FROM --platform=linux/amd64 {PLAYWRIGHT_IMAGE} AS runtime",
    ]
    assert dockerfile.count(APT_TRANSACTION) == 1
    assert len(re.findall(r"\bapt-get\b", dockerfile)) == 2
    assert "ARG TARGETARCH" in dockerfile
    assert 'test "$TARGETARCH" = "amd64"' in dockerfile
    assert "dpkg --print-architecture" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "COPY --chown=10001:10001 . ." in dockerfile
    assert 'ENTRYPOINT ["python", "-m", "ops.testing.browser_session"]' in dockerfile
    command_line = next(
        line.removeprefix("CMD ")
        for line in dockerfile.splitlines()
        if line.startswith("CMD ")
    )
    assert json.loads(command_line) == ["hold"]
    assert 'PYTHONTZPATH=""' in dockerfile
    assert "PLAYWRIGHT_VERSION=1.61.0" in dockerfile
    assert "pytest-playwright==0.8.0" in (PROJECT_ROOT / "pyproject.toml").read_text()


def test_browser_runner_filesystem_has_one_writable_tmpfs() -> None:
    tmpfs_target = PurePosixPath("/", "tmp", "clinic-browser").as_posix()
    assert filesystem_contract() == {
        "ipc_mode": "none",
        "root_read_only": True,
        "shm_size_bytes": 0,
        "tmpfs_mounts": [
            {
                "gid": 10001,
                "mode": 0o700,
                "nodev": True,
                "noexec": True,
                "nosuid": True,
                "size_bytes": 268_435_456,
                "target": tmpfs_target,
                "uid": 10001,
            }
        ],
        "writable_paths": [tmpfs_target],
    }


def test_browser_runner_accepts_nss_help_exit_status() -> None:
    session = (PROJECT_ROOT / "ops/testing/browser_session.py").read_text()

    assert "certutil_version.returncode not in {0, 1, 255}" in session


def test_browser_runner_uses_ipc_none_for_runtime_shm_confinement() -> None:
    probe = (PROJECT_ROOT / "ops/testing/browser_runner_probe.py").read_text()

    assert "{{.HostConfig.IpcMode}}|{{len .HostConfig.Tmpfs}}" in probe
    assert "HostConfig.ShmSize" not in probe


def test_browser_runner_rejects_an_absent_required_suite() -> None:
    assert selected_suites(["patient"], []) == ()
    with pytest.raises(RuntimeError, match="browser suite contract failed"):
        selected_suites([], ["patient"])


def test_runtime_https_dispatch_binds_vector_and_frames_artifacts() -> None:
    claim_id = "11111111-1111-4111-8111-111111111111"
    raw = build_runtime_dispatch_frame(
        claim_id,
        ["runtime-https", "https://phase1a.qa.clinic-os.dev:8443"],
    )

    acknowledgement, frames = dispatch_runtime_frame(
        raw,
        claim_id,
        {"runtime-https": lambda: {"runtime/result.json": b'{"ok":true}\n'}},
    )

    ack = json.loads(acknowledgement)
    manifest = json.loads(frames[0])
    assert ack["accepted"] is True
    assert ack["vector_sha256"] == manifest["vector_sha256"]
    assert manifest["entries"] == [
        {
            "path": "runtime/result.json",
            "sha256": (
                "e5f1eb4d806641698a35efe20e098efd20d7d57a9b90ee69079d5bb650920726"
            ),
            "size_bytes": 12,
        }
    ]
    assert len(frames) == 2


def test_runtime_https_dispatch_rejects_traversal_and_order_drift() -> None:
    claim_id = "11111111-1111-4111-8111-111111111111"
    raw = build_runtime_dispatch_frame(claim_id, ["runtime-https"])

    with pytest.raises(RuntimeError):
        dispatch_runtime_frame(
            raw,
            claim_id,
            {"runtime-https": lambda: {"../escape": b"blocked"}},
        )
    with pytest.raises(RuntimeError):
        dispatch_runtime_frame(
            raw,
            claim_id,
            {"runtime-https": lambda: {"z.json": b"z", "a.json": b"a"}},
        )


def test_stack_service_accepts_bound_image_and_filesystem_contracts() -> None:
    service: JsonObject = {
        "command": ["python", "-m", "ops.testing.browser_session"],
        "environment_contract": {
            "absent_keys": ["FORWARDED_ALLOW_IPS", "GUNICORN_CMD_ARGS"],
            "literal": [{"name": "PYTHONTZPATH", "value": ""}],
            "secret_keys": [],
        },
        "extra_hosts": [],
        "filesystem_contract": filesystem_contract(),
        "gid": 10001,
        "image_contract": {
            "available_suite_ids": [],
            "kind": "browser-runner",
            "revision_sha": "1" * 40,
            "source_entry_count": 1,
            "source_manifest_sha256": "2" * 64,
            "tree_sha": "3" * 40,
        },
        "image_id": f"sha256:{'4' * 64}",
        "name": "browser",
        "network_mode": "none",
        "network_refs": [],
        "published_ports": [],
        "start_policy": "prepared-attest-before-use",
        "uid": 10001,
        "volume_mounts": [],
    }

    assert validate_stack_service(service, set()) == "browser"


def test_browser_runner_cli_rejects_noncanonical_forms() -> None:
    script = PROJECT_ROOT / "ops/testing/browser_runner.sh"
    invalid_forms = (
        ("probe", "extra"),
        ("probe", "--require-suite", "patient", "--require-suite", "patient"),
        ("session",),
        ("session", "--profile", "unknown"),
        ("unknown",),
    )

    for arguments in invalid_forms:
        result = run_process((str(script), *arguments))
        assert result.returncode == 2
        assert "browser-runner: invalid invocation" in result.stderr


def test_browser_session_emits_only_its_framed_result() -> None:
    controller = (PROJECT_ROOT / "ops/testing/browser_runner_controller.py").read_text()

    assert "with redirect_stdout(io.StringIO()):" in controller
