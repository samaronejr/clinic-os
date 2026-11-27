"""Capture the approved synthetic F6 plan on independently bootstrapped databases.

Invoke with the Django owner DSN for a disposable database: blank for a fresh
install, or exactly migrated at the F6 base (not the candidate) for an upgrade.
The capture bypasses the deployment comparison by using Django's raw executor,
but executes the real progress writes, DDL, Django recorder and commit edges.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import django
from apps.tenancy.migration_preflight import boundary, schema_digest
from apps.tenancy.migrations import _resumable_schema as recovery
from django.db import connection
from django.db.backends.signals import connection_created
from django.db.migrations.executor import MigrationExecutor

if TYPE_CHECKING:
    from django.db.backends.base.base import BaseDatabaseWrapper


def capture(db: BaseDatabaseWrapper) -> dict[str, str]:
    """Read each edge after a real commit, never a rolled-back simulation."""
    result: dict[str, str] = {}
    executor = MigrationExecutor(db)
    recorder = executor.recorder
    applied = set(recorder.applied_migrations())
    prior = schema_digest(db)
    result[boundary(applied, None, recorded=recorder.has_table())] = prior
    recorder.ensure_schema()
    prior = schema_digest(db)
    result[boundary(applied, None, recorded=True)] = prior
    step_digests: set[str] = {prior}
    record_step = recovery.record_step

    def observed_commit(
        db: BaseDatabaseWrapper, key: recovery.MigrationKey, step: int
    ) -> None:
        record_step(db, key, step)

        def observe() -> None:
            digest = schema_digest(db)
            if digest in step_digests:
                msg = (
                    f"{'.'.join(key)} step {step} cannot distinguish a missing receipt"
                )
                raise RuntimeError(msg)
            step_digests.add(digest)
            result[boundary(applied, (key, step), recorded=True)] = digest

        db.on_commit(observe)

    def observe_migration(action: str, *args: object) -> None:
        if action == "apply_success":
            migration = args[0]
            if not isinstance(migration, django.db.migrations.Migration):
                msg = "Expected a Django migration"
                raise TypeError(msg)
            applied.add((migration.app_label, migration.name))
            digest = schema_digest(db)
            result[boundary(applied, None, recorded=True)] = digest
            step_digests.clear()
            step_digests.add(digest)

    executor.progress_callback = observe_migration
    with patch.object(recovery, "record_step", observed_commit):
        executor.migrate(executor.loader.graph.leaf_nodes())
    return result


def main() -> None:
    """Write only deterministic schema hashes, never a dump or connection info."""
    # Keep application writes off and use a separately bootstrapped clone.
    django.setup()

    def require_disposable(*, connection: BaseDatabaseWrapper, **_: object) -> None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user")
            row = cursor.fetchone()
        if (
            row is None
            or not row[0].startswith("mig_scratch_")
            or row[1] != "clinic_owner"
        ):
            msg = "Plan capture requires a mig_scratch_* database as clinic_owner"
            raise RuntimeError(msg)

    connection_created.connect(require_disposable, weak=False)
    json.dump(capture(connection), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
