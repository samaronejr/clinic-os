from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
MAKE_BINARY: Final = shutil.which("make")
PROCESS_TIMEOUT_SECONDS: Final = 15
REPORT_ENV: Final = "CLINIC_PROCESS_PROBE_REPORT"
POSTURE_MODE_ENV: Final = "CLINIC_POSTURE_PROBE_MODE"
PROBE_DIRECTORY_ENV: Final = "CLINIC_PSQL_PROBE_DIRECTORY"
PROBE_MODULE_ENV: Final = "CLINIC_PSQL_PROBE_MODULE"
PROBE_PYTHON_ENV: Final = "CLINIC_PSQL_PROBE_PYTHON"
PROCESS_PROBE_PATH: Final = Path(__file__).with_name("makefile_process_probe.py")
PASSWORD_VARIABLES: Final = (
    "CLINIC_OWNER_PASSWORD",
    "CLINIC_APP_PASSWORD",
    "CLINIC_SUPER_PASSWORD",
)


def test_db_bootstrap_keeps_role_credentials_out_of_process_arguments(
    tmp_path: Path,
) -> None:
    # Given: unique role credentials and a Docker probe that observes live argv.
    seed = hashlib.sha256(str(tmp_path).encode()).hexdigest()
    credentials = tuple(
        f"process-probe-{index}-{seed}" for index in range(len(PASSWORD_VARIABLES))
    )
    report_path = tmp_path / "process-probe.tsv"
    environment = os.environ.copy()
    environment[REPORT_ENV] = str(report_path)
    assert MAKE_BINARY is not None

    # When: the real Make bootstrap recipe drives both psql invocations.
    result = subprocess.run(  # noqa: S603 - fixed Make binary and tuple argv.
        (
            MAKE_BINARY,
            "db-bootstrap",
            f"DOCKER={sys.executable} {PROCESS_PROBE_PATH}",
            "POSTGRES_CONTAINER=process-probe",
            "POSTGRES_PORT=5432",
            *(
                f"{name}={value}"
                for name, value in zip(PASSWORD_VARIABLES, credentials, strict=True)
            ),
        ),
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=PROCESS_TIMEOUT_SECONDS,
    )

    # Then: values remain environment data, reach SQL, and never enter argv/output.
    output = result.stdout + result.stderr
    assert not any(value in output for value in credentials), (
        "bootstrap output exposed a credential sentinel"
    )
    assert result.returncode == 0, "the deterministic Docker probe did not complete"
    rows = [line.split("\t") for line in report_path.read_text().splitlines()]
    psql_rows = [row for row in rows if row[0] == "psql"]
    expected_hashes = ",".join(
        hashlib.sha256(value.encode()).hexdigest() for value in credentials
    )
    assert len(psql_rows) == 2, "both database bootstrap passes must execute"
    assert all(row[1:5] == ["1", "1", "1", "1"] for row in psql_rows), (
        "a Docker/psql argv leak or environment-delivery break was observed"
    )
    assert all(row[5] == expected_hashes for row in psql_rows), (
        "the bootstrap process did not receive every role credential"
    )


def test_db_posture_keeps_database_url_out_of_psql_arguments(tmp_path: Path) -> None:
    # Given: a unique credential-bearing URL and a live in-container psql probe.
    seed = hashlib.sha256(str(tmp_path).encode()).hexdigest()
    database_url = f"postgresql://clinic_app:{seed}@localhost:5432/clinic"
    report_path = tmp_path / "posture-process-probe.tsv"
    psql_probe = tmp_path / "psql"
    psql_probe.write_text(
        '#!/bin/sh\nexec "$CLINIC_PSQL_PROBE_PYTHON" '
        '"$CLINIC_PSQL_PROBE_MODULE" --psql "$@"\n'
    )
    psql_probe.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    environment = os.environ.copy()
    environment[REPORT_ENV] = str(report_path)
    environment[POSTURE_MODE_ENV] = "1"
    environment[PROBE_DIRECTORY_ENV] = str(tmp_path)
    environment[PROBE_MODULE_ENV] = str(PROCESS_PROBE_PATH)
    environment[PROBE_PYTHON_ENV] = sys.executable
    assert MAKE_BINARY is not None

    # When: the real posture recipe runs its three psql checks.
    result = subprocess.run(  # noqa: S603 - fixed Make binary and tuple argv.
        (
            MAKE_BINARY,
            "db-posture",
            f"DOCKER={sys.executable} {PROCESS_PROBE_PATH}",
            "POSTGRES_CONTAINER=process-probe",
            "POSTGRES_PORT=5432",
            "POSTGRES_DB=clinic",
            "TEST_DATABASE_NAME=test_clinic",
            f"CLINIC_APP_PASSWORD={seed}",
            f"APP_DATABASE_URL={database_url}",
        ),
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=PROCESS_TIMEOUT_SECONDS,
    )

    # Then: libpq receives connection components and no credential enters argv/output.
    output = result.stdout + result.stderr
    assert not any(value in output for value in (database_url, seed)), (
        "database posture output exposed a credential"
    )
    assert result.returncode == 0, "the deterministic posture probe did not complete"
    rows = [line.split("\t") for line in report_path.read_text().splitlines()]
    expected_values = ("localhost", "5432", "clinic", "clinic_app", seed)
    expected_hash = hashlib.sha256("\0".join(expected_values).encode()).hexdigest()
    assert len(rows) == 3, "all database posture queries must execute"
    assert all(row[0:3] == ["posture-psql", "1", "1"] for row in rows), (
        "a psql argv leak or libpq environment-delivery break was observed"
    )
    assert all(row[3] == expected_hash for row in rows), (
        "the posture processes did not receive the configured libpq components"
    )
