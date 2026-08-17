from __future__ import annotations

import errno
import os
import pty
import secrets
import selectors
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg

if TYPE_CHECKING:
    from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMMAND_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True, slots=True)
class CommandResult:
    exit_code: int
    output: str


def generated_password() -> str:
    return f"Clinic!{secrets.token_urlsafe(24)}Aa7"


def wait_for_totp_commit(
    database_url: str,
    owner_id: UUID,
    device_id: int,
    counter: int,
) -> None:
    """Wait boundedly until a management TOTP decision commits."""
    deadline = time.monotonic() + 10
    while True:
        with psycopg.connect(database_url) as observer:
            observer.execute("SET LOCAL ROLE clinic_app")
            observer.execute(
                "SELECT pg_catalog.set_config('app.current_user_id', %s, true)",
                [str(owner_id)],
            )
            row = observer.execute(
                "SELECT last_t FROM clinic_app.otp_totp_totpdevice WHERE id = %s",
                [device_id],
            ).fetchone()
        if row is not None and row[0] >= counter:
            return
        if time.monotonic() >= deadline:
            message = "management token decision did not commit"
            raise AssertionError(message)


def run_management_command(
    arguments: tuple[str, ...],
    *,
    database_url: str,
    hidden_responses: tuple[tuple[str, str], ...],
) -> CommandResult:
    pid, master_fd = pty.fork()
    if pid == 0:
        environment = os.environ.copy()
        environment["DJANGO_SETTINGS_MODULE"] = "config.settings.test"
        environment["MIGRATION_DATABASE_URL"] = database_url
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        argv = (sys.executable, str(PROJECT_ROOT / "manage.py"), *arguments)
        os.execve(sys.executable, argv, environment)  # noqa: S606

    output = bytearray()
    pending = list(hidden_responses)
    deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
    selector = selectors.DefaultSelector()
    selector.register(master_fd, selectors.EVENT_READ)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                os.close(master_fd)
                _, status = os.waitpid(pid, 0)
                message = f"management command timed out with {status}"
                raise AssertionError(message)
            if not selector.select(remaining):
                continue
            try:
                chunk = os.read(master_fd, 4096)
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            output.extend(chunk)
            if pending and pending[0][0].encode() in output:
                _, response = pending.pop(0)
                os.write(master_fd, response.encode() + b"\n")
    finally:
        selector.close()
        with suppress(OSError):
            os.close(master_fd)
    _, status = os.waitpid(pid, 0)
    return CommandResult(
        exit_code=os.waitstatus_to_exitcode(status),
        output=output.decode("utf-8", errors="replace"),
    )
