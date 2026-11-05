"""Schema release has a bounded work budget separate from Docker metadata."""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from ops.testing import https_probes, isolation_docker_metadata
from ops.testing.https_service_specs import credentials_for
from ops.testing.https_stack_specs import HttpsStackPlan
from ops.testing.isolation_common import IsolationError

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("release_exit", [0, 1])
def test_https_release_uses_its_migration_budget_and_stops_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release_exit: int
) -> None:
    plan = HttpsStackPlan(
        credentials=credentials_for("11111111-1111-4111-8111-111111111111"),
        database_name="clinic_https_fixture_db",
        environments={},
        network_name="clinic_https_fixture_network",
        pgdata_name="clinic_https_fixture_pgdata",
        cleartext_port=18000,
        https_port=18443,
        database_host="source-db",
        phase="source",
        spec={},
    )
    containers = {"database": "a" * 64, "release": "b" * 64}
    steps: list[str] = []

    def execute(
        arguments: tuple[str, ...],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        assert arguments[1:] == (
            "exec",
            containers["release"],
            "/app/ops/container/release.sh",
        )
        assert not check
        assert capture_output
        assert text
        assert timeout == 120
        steps.append("release")
        return subprocess.CompletedProcess(arguments, release_exit, "", "")

    monkeypatch.setattr(shutil, "which", lambda _: sys.executable)
    monkeypatch.setattr(subprocess, "run", execute)
    monkeypatch.setattr(
        https_probes, "inspect_https_runtime", lambda *_: steps.append("inspect")
    )
    monkeypatch.setattr(
        https_probes, "configure_roles", lambda *_: steps.append("roles")
    )
    monkeypatch.setattr(
        https_probes, "_probe_database_tls", lambda *_: steps.append("database-tls")
    )
    monkeypatch.setattr(https_probes, "_probe_https", lambda *_: steps.append("https"))

    if release_exit:
        with pytest.raises(IsolationError, match="Docker metadata command failed"):
            https_probes.run_https_probes(
                tmp_path, plan, containers, tmp_path / "ca.pem"
            )
        assert steps == ["inspect", "roles", "release"]
    else:
        https_probes.run_https_probes(
            tmp_path,
            plan,
            containers,
            tmp_path / "ca.pem",
            browser_probe=lambda _: steps.append("browser"),
        )
        assert steps == [
            "inspect",
            "roles",
            "release",
            "database-tls",
            "https",
            "browser",
        ]


def test_docker_metadata_keeps_its_thirty_second_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def execute(
        arguments: tuple[str, ...],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        assert arguments[1:] == ("container", "ls", "--quiet")
        assert not check
        assert capture_output
        assert text
        assert timeout == 30
        return subprocess.CompletedProcess(arguments, 0, "inventory\n", "")

    monkeypatch.setattr(shutil, "which", lambda _: sys.executable)
    monkeypatch.setattr(subprocess, "run", execute)
    assert (
        isolation_docker_metadata.run_docker_command(("container", "ls", "--quiet"))
        == "inventory\n"
    )
