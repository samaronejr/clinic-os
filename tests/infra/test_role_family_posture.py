"""Runtime posture checks the privileges a role graph can confer."""

from __future__ import annotations

import os
from typing import LiteralString
from uuid import uuid4

import psycopg
import pytest
from ops.db import posture
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict


def _role_names() -> tuple[sql.Identifier, sql.Identifier]:
    suffix = uuid4().hex
    return (
        sql.Identifier("posture_bridge_" + suffix),
        sql.Identifier("posture_leaf_" + suffix),
    )


def test_posture_refuses_transitive_set_only_parameter_authority() -> None:
    app_url = os.environ["APP_DATABASE_URL"]
    test_db = os.environ.get(
        "TEST_DATABASE_NAME", f"test_{conninfo_to_dict(app_url)['dbname']}"
    )
    bridge, leaf = _role_names()
    with psycopg.connect(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], autocommit=True
    ) as admin:
        with admin.transaction():
            admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(leaf))
            admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(bridge))
            admin.execute(
                sql.SQL("GRANT {} TO {} WITH INHERIT FALSE, SET TRUE").format(
                    leaf, bridge
                )
            )
            admin.execute(
                sql.SQL("GRANT SET ON PARAMETER log_parameter_max_length TO {}").format(
                    leaf
                )
            )
        try:
            # The privilege exists but clinic_app cannot yet reach it.
            assert posture._verify_database_posture(app_url, test_db)
            admin.execute(
                sql.SQL("GRANT {} TO clinic_app WITH INHERIT FALSE, SET TRUE").format(
                    bridge
                )
            )
            # A direct/inherited check remains false: it misses the switch.
            assert admin.execute(
                "SELECT has_parameter_privilege("
                "'clinic_app', 'log_parameter_max_length', 'SET')"
            ).fetchone() == (False,)
            assert not posture._verify_database_posture(app_url, test_db)
            admin.execute(sql.SQL("REVOKE {} FROM clinic_app").format(bridge))
            assert posture._verify_database_posture(app_url, test_db)
        finally:
            with admin.transaction():
                admin.execute(
                    sql.SQL(
                        "REVOKE SET ON PARAMETER log_parameter_max_length FROM {}"
                    ).format(leaf)
                )
                # Removing our unique NOLOGIN roles also removes their edges
                # if an assertion failed before the explicit membership revoke.
                admin.execute(sql.SQL("DROP ROLE {}, {}").format(bridge, leaf))


@pytest.mark.parametrize(
    "grant",
    [
        "ALTER ROLE {} SUPERUSER",
        "GRANT SET ON PARAMETER log_error_verbosity TO {}",
        "GRANT SET ON PARAMETER log_statement TO {}",
        "GRANT ALTER SYSTEM ON PARAMETER work_mem TO {}",
        "GRANT pg_read_server_files TO {}",
        "GRANT pg_read_all_settings TO {}",
        "GRANT pg_monitor TO {}",
        "GRANT pg_write_server_files TO {}",
        "GRANT pg_execute_server_program TO {}",
        "GRANT EXECUTE ON FUNCTION pg_catalog.pg_read_file(text) TO {}",
        "GRANT EXECUTE ON FUNCTION pg_catalog.pg_read_binary_file("
        "text,bigint,bigint,boolean) TO {}",
        "GRANT EXECUTE ON FUNCTION pg_catalog.pg_ls_logdir() TO {}",
        "GRANT EXECUTE ON FUNCTION pg_catalog.lo_import(text) TO {}",
    ],
    ids=[
        "superuser",
        "verbosity",
        "statement-logging",
        "alter-system",
        "server-files",
        "settings",
        "monitor",
        "write-files",
        "server-program",
        "read-file",
        "read-binary-overload",
        "log-directory",
        "large-object-import",
    ],
)
def test_posture_rejects_inheritance_after_a_role_switch(grant: LiteralString) -> None:
    # Keep the higher-impact drift invisible to every other session. Exercise
    # the production predicate, never the granted server operation.
    with (
        psycopg.connect(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as admin,
        admin.transaction(force_rollback=True),
    ):
        bridge, leaf = _role_names()
        admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(leaf))
        admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(bridge))
        admin.execute(
            sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET FALSE").format(leaf, bridge)
        )
        admin.execute(
            sql.SQL("GRANT {} TO clinic_app WITH INHERIT FALSE, SET TRUE").format(
                bridge
            )
        )
        params = (list(posture.PINNED_ROLES),)
        assert admin.execute(posture.SAFE_ROLE_FAMILY_QUERY, params).fetchone() == (
            True,
        )
        admin.execute(sql.SQL(grant).format(leaf))
        assert admin.execute(posture.SAFE_ROLE_FAMILY_QUERY, params).fetchone() == (
            False,
        )
        admin.execute(sql.SQL("REVOKE {} FROM clinic_app").format(bridge))
        assert admin.execute(posture.SAFE_ROLE_FAMILY_QUERY, params).fetchone() == (
            True,
        )
