from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAKE_BINARY = shutil.which("make")
MAKE_TIMEOUT_SECONDS = 15


def _run_make(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    assert MAKE_BINARY is not None
    return subprocess.run(  # noqa: S603 - argv is the security boundary under test.
        (MAKE_BINARY, *arguments),
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=MAKE_TIMEOUT_SECONDS,
    )


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


@pytest.mark.parametrize(
    ("variable_name", "payload_template"),
    [
        (
            "TEST_DATABASE_NAME",
            'bad"; touch {canary}; #',
        ),
        (
            "POSTGRES_DB",
            "bad`touch {canary}`",
        ),
        (
            "POSTGRES_DB",
            "bad$(shell touch {canary})",
        ),
        (
            "POSTGRES_USER",
            'bad"\ntouch {canary}\n#',
        ),
        (
            "POSTGRES_PORT",
            '55413"; touch {canary}; #',
        ),
        (
            "POSTGRES_CONTAINER",
            'fixture"; touch {canary}; #',
        ),
    ],
)
def test_db_bootstrap_rejects_instruction_like_identifiers_before_docker(
    tmp_path: Path,
    variable_name: str,
    payload_template: str,
) -> None:
    # Given: a caller-controlled identifier attempts to become Make or shell source
    canary = tmp_path / "identifier-injection-canary"
    payload = payload_template.format(canary=canary)

    # When: the real bootstrap target runs with Docker replaced by an inert command
    result = _run_make(
        (
            "db-bootstrap",
            "DOCKER=true",
            "POSTGRES_CONTAINER=fixture",
            "POSTGRES_PORT=55413",
            f"{variable_name}={payload}",
        )
    )

    # Then: host-side parsing rejects the value before any injected instruction runs
    assert result.returncode != 0, _output(result)
    assert not canary.exists(), _output(result)


@pytest.mark.parametrize(
    "variable_name",
    [
        "POSTGRES_PASSWORD",
        "CLINIC_OWNER_PASSWORD",
        "CLINIC_APP_PASSWORD",
        "CLINIC_SUPER_PASSWORD",
    ],
)
def test_db_bootstrap_treats_passwords_as_opaque_environment_data(
    tmp_path: Path,
    variable_name: str,
) -> None:
    # Given: a password contains syntax that would execute inside recipe source
    canary = tmp_path / "password-injection-canary"
    payload = f"secret`touch {canary}`"

    # When: bootstrap forwards the value while Docker is safely inert
    result = _run_make(
        (
            "db-bootstrap",
            "DOCKER=true",
            "POSTGRES_CONTAINER=fixture",
            "POSTGRES_PORT=55413",
            f"{variable_name}={payload}",
        )
    )

    # Then: the password remains data and the target completes
    assert result.returncode == 0, _output(result)
    assert not canary.exists(), _output(result)


def test_ci_treats_database_urls_as_opaque_environment_data(tmp_path: Path) -> None:
    # Given: every database URL contains shell command-substitution syntax
    canary = tmp_path / "url-injection-canary"
    payload = f"postgresql://role:secret@localhost:55413/clinic`touch {canary}`"

    # When: the CI recipe runs with recursive Make and uv safely inert
    result = _run_make(
        (
            "ci",
            "MAKE=true",
            "UV=true",
            "POSTGRES_CONTAINER=fixture",
            f"APP_DATABASE_URL={payload}",
            f"MIGRATION_DATABASE_URL={payload}",
            f"TEST_SUPERUSER_DATABASE_URL={payload}",
        )
    )

    # Then: URL data is never reparsed into instructions
    assert result.returncode == 0, _output(result)
    assert not canary.exists(), _output(result)


def test_make_dry_run_redacts_passwords_and_database_urls() -> None:
    # Given: unique sentinels stand in for credentials and URLs
    sentinels = (
        "sentinel-postgres-secret",
        "sentinel-owner-secret",
        "sentinel-app-secret",
        "sentinel-super-secret",
        "postgresql://runtime:sentinel-runtime@localhost:55413/clinic",
        "postgresql://owner:sentinel-migration@localhost:55413/clinic",
        "postgresql://super:sentinel-superuser@localhost:55413/clinic",
    )

    # When: Make renders each database-facing recipe without executing it
    bootstrap = _run_make(
        (
            "-n",
            "db-bootstrap",
            "DOCKER=true",
            "POSTGRES_CONTAINER=fixture",
            f"POSTGRES_PASSWORD={sentinels[0]}",
            f"CLINIC_OWNER_PASSWORD={sentinels[1]}",
            f"CLINIC_APP_PASSWORD={sentinels[2]}",
            f"CLINIC_SUPER_PASSWORD={sentinels[3]}",
        )
    )
    posture = _run_make(
        (
            "-n",
            "db-posture",
            "DOCKER=true",
            "POSTGRES_CONTAINER=fixture",
            f"APP_DATABASE_URL={sentinels[4]}",
        )
    )
    ci = _run_make(
        (
            "-n",
            "ci",
            "MAKE=true",
            "UV=true",
            "POSTGRES_CONTAINER=fixture",
            f"MIGRATION_DATABASE_URL={sentinels[5]}",
            f"TEST_SUPERUSER_DATABASE_URL={sentinels[6]}",
        )
    )
    output = _output(bootstrap) + _output(posture) + _output(ci)

    # Then: dry-run diagnostics expose variable names but no credential values
    assert bootstrap.returncode == 0, _output(bootstrap)
    assert posture.returncode == 0, _output(posture)
    assert ci.returncode == 0, _output(ci)
    assert all(sentinel not in output for sentinel in sentinels), output


def test_ci_normal_output_redacts_database_urls() -> None:
    # Given: unique URL sentinels and inert external commands
    sentinels = (
        "postgresql://runtime:normal-runtime@localhost:55413/clinic",
        "postgresql://owner:normal-migration@localhost:55413/clinic",
        "postgresql://super:normal-superuser@localhost:55413/clinic",
    )

    # When: the CI recipe executes normally without contacting external services
    result = _run_make(
        (
            "ci",
            "MAKE=true",
            "UV=true",
            "POSTGRES_CONTAINER=fixture",
            f"APP_DATABASE_URL={sentinels[0]}",
            f"MIGRATION_DATABASE_URL={sentinels[1]}",
            f"TEST_SUPERUSER_DATABASE_URL={sentinels[2]}",
        )
    )
    output = _output(result)

    # Then: ordinary logs reveal no URL credential value
    assert result.returncode == 0, output
    assert all(sentinel not in output for sentinel in sentinels), output
