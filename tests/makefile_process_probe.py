from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

PROCESS_TIMEOUT_SECONDS: Final = 15
REPORT_ENV: Final = "CLINIC_PROCESS_PROBE_REPORT"
POSTURE_MODE_ENV: Final = "CLINIC_POSTURE_PROBE_MODE"
PROBE_DIRECTORY_ENV: Final = "CLINIC_PSQL_PROBE_DIRECTORY"
PROBE_MODULE_ENV: Final = "CLINIC_PSQL_PROBE_MODULE"
PROBE_PYTHON_ENV: Final = "CLINIC_PSQL_PROBE_PYTHON"
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


def _run_posture_psql_probe(arguments: Sequence[str]) -> int:
    libpq_names = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")
    libpq_values = tuple(os.environ.get(name, "") for name in libpq_names)
    command_line = Path("/proc/self/cmdline").read_bytes()
    argv_is_clean = all(
        value.encode() not in command_line
        for value in (os.environ["APP_DATABASE_URL"], os.environ.get("PGPASSWORD", ""))
        if value
    )
    _append_report(
        (
            "posture-psql",
            "1" if argv_is_clean else "0",
            "1" if all(libpq_values) else "0",
            hashlib.sha256("\0".join(libpq_values).encode()).hexdigest(),
        )
    )
    query = arguments[arguments.index("-c") + 1] if "-c" in arguments else arguments[-1]
    if "rolcreatedb" in query:
        sys.stdout.write("f f f\n")
    elif "pg_get_userbyid" in query:
        sys.stdout.write("clinic_owner\n")
    else:
        sys.stdout.write("clinic_app f f\n")
    return 0


def _run_posture_shell(
    environment_arguments: Collection[str],
    command: Sequence[str],
) -> int:
    environment = {name: os.environ[name] for name in environment_arguments}
    environment.update(
        {
            name: os.environ[name]
            for name in (
                REPORT_ENV,
                PROBE_MODULE_ENV,
                PROBE_PYTHON_ENV,
                "APP_DATABASE_URL",
            )
        }
    )
    environment["PATH"] = f"{os.environ[PROBE_DIRECTORY_ENV]}:{os.environ['PATH']}"
    result = subprocess.run(  # noqa: S603 - fixed Make recipe and probe PATH.
        command,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=PROCESS_TIMEOUT_SECONDS,
    )
    return result.returncode


def _run_docker_probe(arguments: Sequence[str]) -> int:
    environment_arguments, command = _parse_docker_exec(arguments)
    if command[0] == "sh" and os.environ.get(POSTURE_MODE_ENV) == "1":
        return _run_posture_shell(environment_arguments, command)
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
    if arguments[0] == "--psql":
        return _run_posture_psql_probe(arguments[1:])
    return _run_docker_probe(arguments)


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))
