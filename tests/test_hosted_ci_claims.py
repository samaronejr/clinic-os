from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, unquote, urlsplit

from ops.testing import ci_postgres_controller
from ops.testing.ci_postgres_runtime import CiDatabaseLease, write_environment

if TYPE_CHECKING:
    import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_ci_browser_suite_file_is_exactly_the_three_host_http_suites() -> None:
    # Given: the tracked Todo-19 suite selector consumed by local and hosted CI.
    path = PROJECT_ROOT / "ops/testing/ci-required-browser-suites.txt"
    assert path.is_file()

    # When / Then: bytes are sorted, LF-terminated, and exclude Todo 20 runtime HTTPS.
    assert path.read_bytes() == b"availability\npatient\nscheduling\n"
    assert not (PROJECT_ROOT / "ops/testing/final-required-browser-suites.txt").exists()


def test_ci_postgres_environment_percent_encodes_database_credentials(
    tmp_path: Path,
) -> None:
    # Given: generated credentials containing URI delimiters.
    path = tmp_path / "ci-postgres.env"
    lease = CiDatabaseLease(
        "Aa1!/?:#@app",
        tmp_path / "ca certificate.pem",
        "claim",
        "container",
        "clinic_test",
        55432,
        "Aa1!/?:#@owner",
        "Aa1!/?:#@postgres",
        "Aa1!/?:#@super",
    )

    # When: the hosted database environment is materialized.
    write_environment(path, lease)
    exports = dict(
        line.removeprefix("export ").split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
    )
    raw_url = exports["TEST_SUPERUSER_DATABASE_URL"].strip("'")
    parsed = urlsplit(raw_url)

    # Then: URI parsing recovers the exact password and loopback TLS parameters.
    assert unquote(parsed.password or "") == lease.super_password
    assert parsed.hostname == "phase1a-db.qa.clinic-os.dev"
    assert parse_qs(parsed.query) == {
        "hostaddr": ["127.0.0.1"],
        "sslmode": ["verify-full"],
        "sslrootcert": [str(lease.ca_path)],
    }


def test_ci_postgres_down_is_idempotent_before_or_after_failed_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a valid runner root without a surviving PostgreSQL state directory.
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))

    # When / Then: cleanup is a successful no-op, so a pre-start trap is safe.
    ci_postgres_controller._down()
