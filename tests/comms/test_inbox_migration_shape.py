"""The inbox schema publishes tables and posture as one irreversible step."""

from __future__ import annotations

from django.db import migrations
from django.db.migrations.loader import MigrationLoader

KEY = ("comms", "0008_omnichannel_inbox")


def test_tables_and_contract_commit_together_and_cannot_be_reversed() -> None:
    migration = MigrationLoader(None).disk_migrations[KEY]
    # The one operation owns its own transaction, so no table outlives a failed
    # contract and none is ever visible without RLS and ACLs.
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
