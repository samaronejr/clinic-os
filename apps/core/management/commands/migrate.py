"""Guard the normal all-leaves deployment command without patching Django."""

from __future__ import annotations

from typing import cast

from django.core.management.base import CommandError
from django.core.management.commands.migrate import Command as DjangoMigrate
from django.db import connections
from django.db.migrations.executor import MigrationExecutor

from apps.tenancy import migration_preflight
from apps.tenancy.migrations._resumable_schema import (
    MigrationRecoveryError,
    allow_verified_resume,
    refuse,
)


class Command(DjangoMigrate):
    """Apply the approved PostgreSQL forward plan with edge verification."""

    def handle(self, *args: object, **options: object) -> str | None:
        """Keep planning, status checks and targeted developer commands native."""
        db = connections[cast("str", options["database"])]
        if db.vendor != "postgresql" or any(
            options[k] for k in ("plan", "check_unapplied", "prune", "app_label")
        ):
            return super().handle(*args, **options)
        try:
            with migration_preflight.migration_lock(db):
                executor = MigrationExecutor(db)
                if options["fake"] or options["fake_initial"]:
                    refuse("fake migration records are outside the approved plan")
                destination, cut = migration_preflight.verify_start(executor)
                with allow_verified_resume(db, cut):
                    result = super().handle(*args, **options)
                migration_preflight.verify_end(db, destination)
        except MigrationRecoveryError as exc:
            raise CommandError(str(exc)) from None
        return result
