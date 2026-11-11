"""Process setup shared by the harness: Django, synthetic secrets, DB roles."""

from __future__ import annotations

import os
import secrets
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

SETTINGS_MODULE = "config.settings.test"
CLIENT_HOST = "localhost"


class PerfEnvironmentError(RuntimeError):
    """Refuse to run outside a throwaway synthetic database."""


def bootstrap_django() -> Path:
    """Configure Django with a private synthetic KEK and return its directory.

    The harness only writes synthetic tenants into the database named by
    ``MIGRATION_DATABASE_URL``; refusing the default port keeps it away from a
    developer's shared database.
    """
    url = os.environ.get("MIGRATION_DATABASE_URL", "")
    if not url or ":5432/" in url:
        message = (
            "set MIGRATION_DATABASE_URL to a private throwaway PostgreSQL "
            "(not port 5432)"
        )
        raise PerfEnvironmentError(message)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", SETTINGS_MODULE)
    os.environ.setdefault("ALLOWED_HOSTS", CLIENT_HOST)
    secret_dir = Path(tempfile.mkdtemp(prefix="clinic-perf-secrets-"))
    secret_dir.chmod(0o700)
    kek = secret_dir / "tenant-kek.secret"
    kek.write_text(secrets.token_hex(32), encoding="ascii")
    kek.chmod(0o600)
    os.environ["CLINIC_SECRET_BACKEND"] = "synthetic-file"  # noqa: S105 - backend name
    os.environ["CLINIC_SECRET_DIR"] = str(secret_dir)
    import django  # noqa: PLC0415 - settings must exist first

    django.setup()
    return secret_dir


@contextmanager
def runtime_role() -> Iterator[None]:
    """Run the block as the application role so RLS and grants are real."""
    from django.db import connection  # noqa: PLC0415

    with connection.cursor() as cursor:
        cursor.execute("SET ROLE clinic_app")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")


@contextmanager
def slow_queries(milliseconds: int, markers: tuple[str, ...]) -> Iterator[None]:
    """QA fault injection: delay statements containing a marker, inside the database.

    A zero delay installs nothing. The delay is a real ``pg_sleep`` before the
    statement, so the recorded latency is a genuine slow query.
    """
    if milliseconds <= 0:
        yield
        return
    from django.db import connection  # noqa: PLC0415

    def wrapper(
        execute: Callable[..., object],
        sql: str,
        params: object,
        many: bool,
        context: dict[str, Any],
    ) -> object:
        if any(marker in sql for marker in markers) and "pg_sleep" not in sql:
            context["cursor"].cursor.execute(
                "SELECT pg_sleep(%s)", [milliseconds / 1000.0]
            )
        return execute(sql, params, many, context)

    with connection.execute_wrapper(wrapper):
        yield
