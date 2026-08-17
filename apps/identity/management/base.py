"""Secret-safe owner database command primitives."""

from __future__ import annotations

import getpass
import os
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from django.core.management import BaseCommand, CommandError
from django.db import connection

if TYPE_CHECKING:
    from collections.abc import Iterator
    from io import TextIOWrapper

type CommandOption = str | int | bool | None | list[str]


class LifecycleCommandError(Exception):
    """Reject one lifecycle command without reflecting its inputs."""


class _OwnerRoleRequiredError(LifecycleCommandError):
    pass


class _ControllingTtyRequiredError(LifecycleCommandError):
    pass


class _HiddenInputError(LifecycleCommandError):
    pass


def required_text(options: dict[str, CommandOption], name: str) -> str:
    """Return one required nonblank text option."""
    value = options.get(name)
    if not isinstance(value, str) or not value:
        raise LifecycleCommandError
    return value


def required_uuid(options: dict[str, CommandOption], name: str) -> UUID:
    """Parse one required UUID option without reflecting its value."""
    try:
        return UUID(required_text(options, name))
    except ValueError as error:
        raise LifecycleCommandError from error


def assert_owner_database_role() -> None:
    """Require the dedicated owner database role."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        row = cursor.fetchone()
    if row != ("clinic_owner",):
        raise _OwnerRoleRequiredError


@contextmanager
def owner_tty() -> Iterator[TextIOWrapper]:
    """Open the controlling TTY before any database access."""
    try:
        with Path("/dev/tty").open("w", encoding="utf-8", buffering=1) as tty:
            if not os.isatty(tty.fileno()):
                raise _ControllingTtyRequiredError
            assert_owner_database_role()
            yield tty
    except OSError as error:
        raise _ControllingTtyRequiredError from error


def hidden_input(tty: TextIOWrapper, prompt: str) -> str:
    """Read one nonblank hidden value from the controlling TTY."""
    try:
        value = getpass.getpass(prompt, stream=tty)
    except (EOFError, KeyboardInterrupt) as error:
        raise _HiddenInputError from error
    if not value:
        raise _HiddenInputError
    return value


class OwnerDatabaseCommand(BaseCommand):
    """Map typed lifecycle failures to one payload-free command error."""

    def command_error(self) -> CommandError:
        """Return the sole payload-free public command failure."""
        return CommandError("owner lifecycle command failed")
