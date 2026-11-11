from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

import pytest
from ops.testing import ci_postgres_controller, ci_postgres_runtime
from ops.testing.ci_postgres_runtime import (
    CiDatabaseLease,
    ci_database_lease,
    write_environment,
)
from ops.testing.ci_postgres_settings import TEST_DATABASE_SETTINGS

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject
    from ops.testing.tls_export import PublicCaExport
    from ops.testing.tls_materializer import MaterializerLease

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_ci_browser_suite_file_includes_the_required_renewal_suite() -> None:
    # Given: the tracked CI selector with the Wave B clinical inbox extension.
    path = PROJECT_ROOT / "ops/testing/ci-required-browser-suites.txt"
    assert path.is_file()

    # When / Then: bytes are sorted, LF-terminated, and exclude Todo 20 runtime HTTPS.
    assert path.read_bytes() == b"availability\nclinical-inbox\npatient\nscheduling\n"
    assert (
        PROJECT_ROOT / "ops/testing/final-required-browser-suites.txt"
    ).read_bytes() == (b"availability\npatient\nruntime-https\nscheduling\n")


def test_ci_browser_contract_dispatches_image_and_renewal_suites(
    tmp_path: Path,
) -> None:
    _ = (tmp_path / "Makefile").write_bytes((PROJECT_ROOT / "Makefile").read_bytes())
    testing = tmp_path / "ops/testing"
    testing.mkdir(parents=True)
    _ = (testing / "ci-required-browser-suites.txt").write_bytes(
        (PROJECT_ROOT / "ops/testing/ci-required-browser-suites.txt").read_bytes()
    )
    probe = testing / "browser_runner.sh"
    _ = probe.write_text("#!/bin/sh\nprintf 'legacy:%s\\n' \"$@\"\n", encoding="utf-8")
    probe.chmod(0o700)

    result = subprocess.run(
        (
            "/usr/bin/make",
            "--no-print-directory",
            "ci-browser-contract",
            "POSTGRES_CONTAINER=synthetic",
            "UV=printf 'renewal:%s\\n'",
        ),
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "legacy:probe",
        "legacy:--require-suite",
        "legacy:availability",
        "legacy:--require-suite",
        "legacy:patient",
        "legacy:--require-suite",
        "legacy:scheduling",
        "renewal:run",
        "renewal:--frozen",
        "renewal:--no-sync",
        "renewal:--no-env-file",
        "renewal:python",
        "renewal:-m",
        "renewal:ops.testing.renewal_runner",
        "renewal:browser",
        "renewal:--suite",
        "renewal:clinical-inbox",
    ]


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


class _ReservedError(Exception):
    """Stop a lease at its claim reservation, before any Docker resource."""


def _command(spec: JsonObject) -> list[str]:
    desired = cast("JsonObject", spec["desired"])
    [service] = cast("list[JsonObject]", desired["services"])
    return cast("list[str]", service["command"])


def test_only_the_hosted_quality_database_adds_the_suite_lock_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: leases stopped at their claim reservation, which records the spec.
    ledger = tmp_path / ".omo/evidence/isolation-ledger-phase1a.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("{}\n")
    reserved: list[JsonObject] = []

    def reserve(_ledger: Path, spec: JsonObject) -> None:
        reserved.append(spec)
        raise _ReservedError

    monkeypatch.setattr(ci_postgres_runtime, "_reserve", reserve)
    materializer = cast(
        "MaterializerLease",
        SimpleNamespace(
            claim_id=str(uuid4()),
            image_id="sha256:" + "0" * 64,
            volume_names={
                "phase1a-source-db-tls": "source-tls",
                "phase1a-restore-db-tls": "restore-tls",
            },
        ),
    )
    public_ca = cast(
        "PublicCaExport",
        SimpleNamespace(claim_id=str(uuid4()), path=tmp_path / "ca.pem"),
    )
    cases: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("source", TEST_DATABASE_SETTINGS),
        ("source", ()),
        ("restore", ()),
    )

    # When: the hosted daemon's lease and the restore rehearsal's two are prepared.
    for kind, settings in cases:
        with (
            pytest.raises(_ReservedError),
            ci_database_lease(
                tmp_path, materializer, public_ca, kind, server_settings=settings
            ),
        ):
            pass
    quality, source, restore = (_command(spec) for spec in reserved)

    # Then: only the quality database extends the shared TLS server command.
    assert quality == [*source, *TEST_DATABASE_SETTINGS]
    assert restore == source
    assert not any("max_locks_per_transaction" in argument for argument in source)
