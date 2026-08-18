"""Fail-closed owner-role preflight for schema release processes."""

from __future__ import annotations

import django
from django.db import connection


def assert_owner_connection() -> None:
    """Require the authenticated PostgreSQL session to be clinic_owner."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        current = cursor.fetchone()
    if current != ("clinic_owner",):
        message = "release owner contract failed"
        raise _ReleaseOwnerError(message)


class _ReleaseOwnerError(RuntimeError):
    pass


def _main() -> None:
    django.setup()
    assert_owner_connection()


if __name__ == "__main__":
    _main()
