"""Process setup shared by the harness: Django, synthetic secrets, DB roles."""

from __future__ import annotations

import os
import secrets
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from ops.testing.runtime_paths import runtime_directory

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

SETTINGS_MODULE = "config.settings.test"
CLIENT_HOST = "localhost"


class PerfEnvironmentError(RuntimeError):
    """Refuse to run outside a throwaway synthetic database."""


@contextmanager
def bootstrap_django(run_root: Path | None) -> Iterator[Path]:
    """Own Django's synthetic key/media directory for the entire run.

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
    if run_root is None:
        message = "set --run-root to an existing private directory"
        raise PerfEnvironmentError(message)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", SETTINGS_MODULE)
    os.environ.setdefault("ALLOWED_HOSTS", CLIENT_HOST)
    names = ("CLINIC_SECRET_BACKEND", "CLINIC_SECRET_DIR", "SCRIBE_MEDIA_ROOT")
    previous = {name: os.environ.get(name) for name in names}
    with runtime_directory(run_root, purpose="perf-secrets") as secret_dir:
        kek = secret_dir / "tenant-kek.secret"
        kek.write_text(secrets.token_hex(32), encoding="ascii")
        kek.chmod(0o600)
        try:
            os.environ["CLINIC_SECRET_BACKEND"] = "synthetic-file"  # noqa: S105 - backend name
            os.environ["CLINIC_SECRET_DIR"] = str(secret_dir)
            os.environ["SCRIBE_MEDIA_ROOT"] = str(secret_dir / "media")
            import django  # noqa: PLC0415 - settings must exist first

            django.setup()
            yield secret_dir
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


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
