from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

PROCESS_TIMEOUT_SECONDS: Final = 15
REPORT_ENV: Final = "CLINIC_PROCESS_PROBE_REPORT"
PASSWORD_VARIABLES: Final = (
    "CLINIC_OWNER_PASSWORD",
    "CLINIC_APP_PASSWORD",
    "CLINIC_SUPER_PASSWORD",
)
PSQL_VARIABLES: Final = (
    ("clinic_owner_password", "CLINIC_OWNER_PASSWORD"),
    ("clinic_app_password", "CLINIC_APP_PASSWORD"),
    ("clinic_super_password", "CLINIC_SUPER_PASSWORD"),
)


def _parse_docker_exec(
    arguments: Sequence[str],
) -> tuple[frozenset[str], tuple[str, ...]]:
    assert arguments
    assert arguments[0] == "exec"
    environment_arguments: set[str] = set()
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        if argument == "-i":
            index += 1
            continue
        if argument == "-e":
            environment_arguments.add(arguments[index + 1])
            index += 2
            continue
        return frozenset(environment_arguments), tuple(arguments[index + 1 :])
    raise AssertionError


def _contains_no_credentials(command_line: bytes, credentials: Sequence[bytes]) -> bool:
    return all(credential not in command_line for credential in credentials)


def _probe_psql_process(command: Sequence[str]) -> bytes:
    child = subprocess.Popen(  # noqa: S603 - controlled live argv probe.
        (
            sys.executable,
            "-c",
            "import sys; sys.stdin.buffer.read(1)",
            *command,
        ),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        command_line = Path(f"/proc/{child.pid}/cmdline").read_bytes()
    finally:
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=PROCESS_TIMEOUT_SECONDS)
    return command_line


def _append_report(fields: Sequence[str]) -> None:
    report_path = Path(os.environ[REPORT_ENV])
    with report_path.open("a", encoding="utf-8") as report:
        report.write("\t".join(fields) + "\n")


def _run_uv_probe(arguments: Sequence[str]) -> int:
    database_url = os.environ.get("APP_DATABASE_URL", "")
    command_line = Path("/proc/self/cmdline").read_bytes()
    argv_is_clean = bool(database_url) and database_url.encode() not in command_line
    expected_command = tuple(arguments) == ("run", "python", "ops/db/posture.py")
    _append_report(
        (
            "posture-python",
            "1" if argv_is_clean else "0",
            "1" if expected_command else "0",
            hashlib.sha256(database_url.encode()).hexdigest(),
        )
    )
    return 0 if argv_is_clean and expected_command else 97


def _run_docker_probe(arguments: Sequence[str]) -> int:
    environment_arguments, command = _parse_docker_exec(arguments)
    credentials = tuple(os.environ[name].encode() for name in PASSWORD_VARIABLES)
    self_command_line = Path("/proc/self/cmdline").read_bytes()
    self_is_clean = _contains_no_credentials(self_command_line, credentials)

    report_fields = [command[0], "1" if self_is_clean else "0", "1", "1", "1", ""]
    if command[0] == "psql":
        child_command_line = _probe_psql_process(command)
        child_is_clean = _contains_no_credentials(child_command_line, credentials)
        names_are_forwarded = all(
            name in environment_arguments for name in PASSWORD_VARIABLES
        )
        bootstrap_sql = sys.stdin.buffer.read()
        sql_imports_environment = all(
            f"\\getenv {psql_name} {environment_name}".encode() in bootstrap_sql
            for psql_name, environment_name in PSQL_VARIABLES
        )
        credential_hashes = ",".join(
            hashlib.sha256(credential).hexdigest() for credential in credentials
        )
        report_fields = [
            "psql",
            "1" if self_is_clean else "0",
            "1" if child_is_clean else "0",
            "1" if names_are_forwarded else "0",
            "1" if sql_imports_environment else "0",
            credential_hashes,
        ]

    _append_report(report_fields)
    return 0


def main(arguments: Sequence[str]) -> int:
    if arguments[0] == "--uv":
        return _run_uv_probe(arguments[1:])
    return _run_docker_probe(arguments)


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))
