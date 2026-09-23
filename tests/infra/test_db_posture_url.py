from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Final
from urllib.parse import quote, urlsplit, urlunsplit

import pytest

from database_urls import database_url_for_name

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
MAKE_BINARY: Final = shutil.which("make")
MAKE_TIMEOUT_SECONDS: Final = 15
INVALID_CREDENTIAL: Final = "posture-credential-must-be-rejected"
WRONG_DATABASE: Final = "postgres"


def _replace_password(database_url: str, password: str) -> str:
    parsed_url = urlsplit(database_url)
    username = quote(parsed_url.username or "", safe="")
    hostname = parsed_url.hostname or ""
    encoded_hostname = f"[{hostname}]" if ":" in hostname else hostname
    port = f":{parsed_url.port}" if parsed_url.port is not None else ""
    credentials = f"{username}:{quote(password, safe='')}"
    return urlunsplit(
        parsed_url._replace(netloc=f"{credentials}@{encoded_hostname}{port}")
    )


def _run_posture(database_url: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "APP_DATABASE_URL": database_url,
            "CLINIC_APP_PASSWORD": "component-password-must-be-ignored",
            "POSTGRES_CONTAINER": "unused-by-posture",
            "POSTGRES_DB": "component_database_must_be_ignored",
            "POSTGRES_PORT": "1",
            "TEST_DATABASE_NAME": os.environ.get("TEST_DATABASE_NAME", "test_clinic"),
        }
    )
    assert MAKE_BINARY is not None
    return subprocess.run(  # noqa: S603 - fixed Make binary and tuple argv.
        (MAKE_BINARY, "db-posture", "DOCKER=true"),
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=MAKE_TIMEOUT_SECONDS,
    )


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def _configured_url(case_name: str) -> str:
    app_database_url = os.environ["APP_DATABASE_URL"]
    if case_name == "owner_role":
        return os.environ["MIGRATION_DATABASE_URL"]
    if case_name == "empty":
        return ""
    if case_name == "malformed":
        return "not-a-postgresql-url"
    if case_name == "wrong_password":
        return _replace_password(app_database_url, INVALID_CREDENTIAL)
    if case_name == "wrong_database":
        return database_url_for_name(app_database_url, WRONG_DATABASE)
    raise AssertionError(case_name)


def test_db_posture_authenticates_the_supplied_app_database_url() -> None:
    # Given: the configured runtime URL and deliberately unrelated Make components
    database_url = os.environ["APP_DATABASE_URL"]

    # When: the posture target checks the live PostgreSQL service
    result = _run_posture(database_url)
    output = _output(result)

    # Then: that exact URL authenticates as the constrained runtime role
    assert result.returncode == 0, output
    assert database_url not in output
    password = urlsplit(database_url).password
    assert password is None or password not in output


@pytest.mark.parametrize(
    "case_name",
    ["owner_role", "empty", "malformed", "wrong_password", "wrong_database"],
)
def test_db_posture_rejects_an_invalid_supplied_database_url(
    case_name: str,
) -> None:
    # Given: one invalid form of the caller-supplied runtime URL
    database_url = _configured_url(case_name)

    # When: the posture target checks only that URL
    result = _run_posture(database_url)
    output = _output(result)

    # Then: authentication/posture fails without echoing URL credentials
    assert result.returncode != 0, output
    assert "database posture check failed" in output
    assert not database_url or database_url not in output
    password = urlsplit(database_url).password
    assert password is None or password not in output
