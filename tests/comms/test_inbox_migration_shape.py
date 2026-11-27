"""The inbox schema publishes tables and posture as one irreversible step."""

from __future__ import annotations

from apps.tenancy.migrations._resumable_schema import GuardedMigration
from django.db import migrations
from django.db.migrations.loader import MigrationLoader

KEY = ("comms", "0008_omnichannel_inbox")


def test_tables_and_contract_commit_together_and_cannot_be_reversed() -> None:
    migration = MigrationLoader(None).disk_migrations[KEY]
    # Tables and authority publish in one journaled step before Django records
    # the migration. The step also grants access to an earlier table.
    assert isinstance(migration, GuardedMigration)
    assert migration.atomic is False
    (operation,) = migration.operations
    assert isinstance(operation, migrations.SeparateDatabaseAndState)
    assert operation.atomic is True
    assert operation.reversible is False
    kinds = [type(step) for step in operation.database_operations]
    assert kinds[-1] is migrations.RunSQL
    assert migrations.RunSQL not in kinds[:-1]
    assert not any(
        isinstance(step, migrations.RunSQL) for step in operation.state_operations
    )
