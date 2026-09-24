from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from phase1_audit_chain_support import (
    append_concurrently,
    append_fixed_matrix,
    assert_reverse_reapply,
    assert_rollback_gap,
    historical_rows,
    tenant_matrix_count,
    verify_new_chains,
)
from phase1_audit_sql_support import (
    AUDIT_V1,
    AUDIT_V2,
    assert_preallocation_rejections,
    assert_v2_catalog,
    database_url,
    seed_v1,
)
from test_intake_migrations import _default_connection, _scratch_database


@pytest.mark.django_db(transaction=True)
def test_v2_append_contract_is_reversible_metadata_only_and_chain_safe(
    superuser_database_url: str,
) -> None:
    # The protected-field migrations are irreversible, so the v2 reverse
    # leg runs on a scratch database migrated forward to the v2 boundary:
    # the unapply plan then contains only the reversible audit migration.
    with (
        _scratch_database(superuser_database_url) as wrapper,
        _default_connection(wrapper),
    ):
        app_database_url = database_url("APP_DATABASE_URL")
        owner_database_url = database_url("MIGRATION_DATABASE_URL")
        executor = MigrationExecutor(connection)
        executor.migrate([AUDIT_V1])
        old_seq, old_event = seed_v1(app_database_url)
        executor = MigrationExecutor(connection)
        executor.migrate([AUDIT_V2])
        assert_v2_catalog()
        assert_preallocation_rejections(app_database_url, owner_database_url)
        append_fixed_matrix()
        concurrent_seqs = append_concurrently(app_database_url)
        assert concurrent_seqs[0] < concurrent_seqs[1]
        row_count, chain_tip = verify_new_chains()
        assert row_count == tenant_matrix_count() + 3
        assert_rollback_gap(app_database_url, chain_tip)
        rows_before_reverse = historical_rows()
        assert_reverse_reapply(
            app_database_url,
            old_event,
            old_seq,
            rows_before_reverse,
        )
