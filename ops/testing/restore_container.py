"""Run every logical-recovery PostgreSQL client inside its owned container."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Final, Never, Protocol

from ops.testing.restore_contract import (
    POSTGRES_VERSION,
    RestoreContractError,
    dump_argv,
    restore_argv,
)

DOCKER: Final = "/usr/bin/docker"
TIMEOUT_SECONDS: Final = 1800
CONTAINER_ID_HEX_LENGTH: Final = 64
POSTGRES_IDENTIFIER_MAX_LENGTH: Final = 63
POSTGRES_VERSION_NUM: Final = "160014"


class _ContainerRunner(Protocol):
    def __call__(
        self,
        argv: tuple[str, ...],
        standard_input: bytes | None,
        environment: dict[str, str],
    ) -> bytes: ...


class _ProbeRunner(Protocol):
    def __call__(
        self,
        argv: tuple[str, ...],
        standard_input: bytes | None,
        environment: dict[str, str],
    ) -> tuple[int, str, str]: ...


@dataclass(frozen=True, slots=True)
class VersionEvidence:
    """Record exact source/target client and server patch versions."""

    dump_client: str
    restore_client: str
    server: str


@dataclass(frozen=True, slots=True)
class ContainerPostgres:
    """Bind data transport to one authenticated task-owned container."""

    container_id: str
    database: str
    password: str
    role: str = "clinic_super"
    runner: _ContainerRunner | None = None
    probe_runner: _ProbeRunner | None = None

    def __post_init__(self) -> None:
        """Reject identifiers or credentials that cannot enter closed argv/env."""
        if (
            len(self.container_id) != CONTAINER_ID_HEX_LENGTH
            or any(
                character not in "0123456789abcdef" for character in self.container_id
            )
            or not self.database
            or len(self.database) > POSTGRES_IDENTIFIER_MAX_LENGTH
            or not self.database.replace("_", "a").isalnum()
            or not self.password
            or "\x00" in self.password
            or self.role not in {"clinic_app", "clinic_owner", "clinic_super"}
        ):
            _fail("container PostgreSQL binding is invalid")
        if self.runner is not None and not callable(self.runner):
            _fail("container PostgreSQL runner is invalid")

    def for_role(self, role: str, password: str) -> ContainerPostgres:
        """Rebind this container to one different authenticated role."""
        return ContainerPostgres(
            container_id=self.container_id,
            database=self.database,
            password=password,
            role=role,
            runner=self.runner,
            probe_runner=self.probe_runner,
        )

    def require_postgresql_16_14(self) -> VersionEvidence:
        """Require both container clients and the server to be exactly 16.14."""
        dump_version = _client_version(
            self.execute(("pg_dump", "--version")), "pg_dump"
        )
        restore_version = _client_version(
            self.execute(("pg_restore", "--version")), "pg_restore"
        )
        server_version = _server_version(self.sql("SHOW server_version_num").strip())
        evidence = VersionEvidence(dump_version, restore_version, server_version)
        if evidence != VersionEvidence(
            POSTGRES_VERSION,
            POSTGRES_VERSION,
            POSTGRES_VERSION,
        ):
            _fail("PostgreSQL client/server version is not exactly 16.14")
        return evidence

    def dump(self) -> bytes:
        """Stream the fixed custom archive from this source container."""
        return self.execute(dump_argv(self.database))

    def list_archive(self, archive: bytes) -> str:
        """List one custom archive using this container's pg_restore client."""
        raw = self.execute(("pg_restore", "--list"), archive)
        return raw.decode("utf-8")

    def restore(self, archive: bytes) -> None:
        """Apply the strict fixed data-only archive to this target container."""
        _ = self.execute(restore_argv(self.database), archive)

    def sql(self, statement: str) -> str:
        """Run one noninteractive exact-database SQL observation."""
        if not statement or "\x00" in statement:
            _fail("container SQL statement is invalid")
        raw = self.execute(
            (
                "psql",
                "--no-psqlrc",
                "--set=ON_ERROR_STOP=1",
                "--tuples-only",
                "--no-align",
                "--host=127.0.0.1",
                f"--username={self.role}",
                f"--dbname={self.database}",
                "--command",
                statement,
            )
        )
        return raw.decode("utf-8")

    def try_sql(self, statement: str) -> tuple[int, str, str]:
        """Run one statement and return (exit code, stdout, stderr).

        Denial probes must observe the database's own refusal, so a nonzero
        exit is data here, not a transport failure.
        """
        if not statement or "\x00" in statement:
            _fail("container SQL statement is invalid")
        argv = (
            DOCKER,
            "exec",
            "-i",
            "--env",
            "PGPASSWORD",
            self.container_id,
            "psql",
            "--no-psqlrc",
            "--set=ON_ERROR_STOP=1",
            "--tuples-only",
            "--no-align",
            "--host=127.0.0.1",
            f"--username={self.role}",
            f"--dbname={self.database}",
            "--command",
            statement,
        )
        environment = {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PGPASSWORD": self.password,
            "TZ": "UTC",
        }
        selected = (
            self.probe_runner if self.probe_runner is not None else run_container_probe
        )
        if not callable(selected):
            _fail("container PostgreSQL runner is invalid")
        return selected(argv, None, environment)

    def execute(
        self, inner: tuple[str, ...], standard_input: bytes | None = None
    ) -> bytes:
        """Execute one fixed inner argv through absolute Docker with a private env."""
        if not inner or any(not item or "\x00" in item for item in inner):
            _fail("container PostgreSQL command is invalid")
        argv = (DOCKER, "exec", "-i", "--env", "PGPASSWORD", self.container_id, *inner)
        environment = {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PGPASSWORD": self.password,
            "TZ": "UTC",
        }
        selected = self.runner if self.runner is not None else run_container_command
        if not callable(selected):
            _fail("container PostgreSQL runner is invalid")
        result = selected(argv, standard_input, environment)
        if not isinstance(result, bytes):
            _fail("container PostgreSQL output is invalid")
        return result


def run_container_command(
    argv: tuple[str, ...],
    standard_input: bytes | None,
    environment: dict[str, str],
) -> bytes:
    """Run one bounded Docker exec and return stdout only on exit zero."""
    return asyncio.run(_run_container_command(argv, standard_input, environment))


def run_container_probe(
    argv: tuple[str, ...],
    standard_input: bytes | None,
    environment: dict[str, str],
) -> tuple[int, str, str]:
    """Run one bounded Docker exec and return exit code and both streams."""
    return asyncio.run(_run_container_probe(argv, standard_input, environment))


async def _run_container_probe(
    argv: tuple[str, ...],
    standard_input: bytes | None,
    environment: dict[str, str],
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(standard_input),
            timeout=TIMEOUT_SECONDS,
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        _fail("container PostgreSQL command timed out")
    if process.returncode is None:
        _fail("container PostgreSQL command did not exit")
    return (
        process.returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def _run_container_command(
    argv: tuple[str, ...],
    standard_input: bytes | None,
    environment: dict[str, str],
) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(standard_input),
            timeout=TIMEOUT_SECONDS,
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        _fail("container PostgreSQL command timed out")
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace")[-1000:]
        _fail(f"container PostgreSQL command failed: {detail}")
    return stdout


def _client_version(raw: bytes, executable: str) -> str:
    prefix = f"{executable} (PostgreSQL) ".encode("ascii")
    if not raw.startswith(prefix) or not raw.endswith(b"\n"):
        _fail("PostgreSQL client version output is invalid")
    return raw.removeprefix(prefix).decode("ascii").split(maxsplit=1)[0]


def _server_version(raw: str) -> str:
    if raw != POSTGRES_VERSION_NUM:
        _fail("PostgreSQL server version output is invalid")
    return POSTGRES_VERSION


def _fail(message: str) -> Never:
    raise RestoreContractError(message)
