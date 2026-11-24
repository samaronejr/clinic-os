"""Replay the original teleconsult DDL at its real, unfenced graph boundary."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from apps.scribe.migrations import _ai_disclosure_sql
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor

from identity.test_enterprise_migration import BEFORE, LANE, _catalog

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def before_enterprise_fence() -> Iterator[None]:
    # Newer AI/export policies legitimately depend on identity.0051. Let the
    # executor reverse its actual installed dependants, not a hand-picked list.
    # No globally visible rollback of the consent or enterprise policies.
    with transaction.atomic():
        executor = MigrationExecutor(connection)
        original = _catalog()
        installed = set(executor.loader.applied_migrations)
        leaves = executor.loader.graph.leaf_nodes()
        plan = executor.migration_plan([BEFORE])
        assert plan
        assert all(back and migration.atomic for migration, back in plan)
        assert all(
            op.reversible for migration, _back in plan for op in migration.operations
        )
        assert (plan[-1][0].app_label, plan[-1][0].name) == LANE
        executor.migrate([BEFORE], plan=plan)
        assert LANE not in MigrationExecutor(connection).loader.applied_migrations
        # 0007 drops/recreates the room predicate. Replay only that definition
        # from scribe.0002: its other trigger was superseded by irreversible
        # retention.0005 and must never revert, even inside this test.
        assert _ai_disclosure_sql._previous_room in _ai_disclosure_sql.REVERSE_SQL
        assert _ai_disclosure_sql._current_room in _ai_disclosure_sql.SQL
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE clinic_resolver")
            cursor.execute(_ai_disclosure_sql._previous_room)
            cursor.execute("RESET ROLE")
        yield
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE clinic_resolver")
            cursor.execute(_ai_disclosure_sql._current_room)
            cursor.execute("RESET ROLE")
        MigrationExecutor(connection).migrate(leaves)
        assert set(MigrationExecutor(connection).loader.applied_migrations) == installed
        assert _catalog() == original
        transaction.set_rollback(True)
