"""Direct, provisioned PG logins: Django test transactions do not span databases."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, LiteralString
from uuid import uuid4

import psycopg
import pytest
from ops.db import posture
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

if TYPE_CHECKING:
    from collections.abc import Iterator


def _create_roles(admin: psycopg.Connection) -> tuple[sql.Identifier, sql.Identifier]:
    suffix = uuid4().hex
    bridge, leaf = (
        sql.Identifier("posture_bridge_" + suffix),
        sql.Identifier("posture_leaf_" + suffix),
    )
    for role in (bridge, leaf):
        admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(role))
    return bridge, leaf


def test_posture_refuses_transitive_set_only_parameter_authority() -> None:
    app_url = os.environ["APP_DATABASE_URL"]
    test_db = os.environ.get(
        "TEST_DATABASE_NAME", f"test_{conninfo_to_dict(app_url)['dbname']}"
    )
    with psycopg.connect(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], autocommit=True
    ) as admin:
        with admin.transaction():
            bridge, leaf = _create_roles(admin)
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
        bridge, leaf = _create_roles(admin)
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


@pytest.mark.parametrize(
    ("grant", "undo"),
    [
        ("GRANT pg_signal_backend TO {}", "REVOKE pg_signal_backend FROM {}"),
        ("GRANT pg_read_all_stats TO {}", "REVOKE pg_read_all_stats FROM {}"),
        ("ALTER ROLE {} CREATEROLE", "ALTER ROLE {} NOCREATEROLE"),
        ("ALTER ROLE {} BYPASSRLS", "ALTER ROLE {} NOBYPASSRLS"),
        (
            "GRANT EXECUTE ON FUNCTION pg_catalog.lo_export(oid,text) TO {}",
            "REVOKE EXECUTE ON FUNCTION pg_catalog.lo_export(oid,text) FROM {}",
        ),
        (
            "GRANT EXECUTE ON FUNCTION pg_catalog.pg_ls_dir(text,boolean,boolean) "
            "TO {}",
            "REVOKE EXECUTE ON FUNCTION pg_catalog.pg_ls_dir(text,boolean,boolean) "
            "FROM {}",
        ),
        (
            "GRANT EXECUTE ON FUNCTION pg_catalog.pg_stat_file(text,boolean) TO {}",
            "REVOKE EXECUTE ON FUNCTION pg_catalog.pg_stat_file(text,boolean) FROM {}",
        ),
    ],
    ids=["signal", "stats", "createrole", "bypassrls", "export", "ls-dir", "stat-file"],
)
def test_grant_and_revoke_behind_admin_only_edge(
    grant: LiteralString, undo: LiteralString
) -> None:
    with (
        psycopg.connect(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as admin,
        admin.transaction(force_rollback=True),
    ):
        bridge, leaf = _create_roles(admin)
        admin.execute(
            sql.SQL("GRANT {} TO {} WITH ADMIN TRUE, INHERIT FALSE, SET FALSE").format(
                leaf, bridge
            )
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
        admin.execute(sql.SQL(undo).format(leaf))
        assert admin.execute(posture.SAFE_ROLE_FAMILY_QUERY, params).fetchone() == (
            True,
        )


def test_migration_resolver_exception_is_not_an_app_exception() -> None:
    with (
        psycopg.connect(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as admin,
        admin.transaction(force_rollback=True),
    ):
        params = (list(posture.PINNED_ROLES),)
        assert admin.execute(posture.SAFE_ROLE_FAMILY_QUERY, params).fetchone() == (
            True,
        )
        admin.execute(
            "GRANT clinic_resolver TO clinic_app WITH INHERIT FALSE, SET TRUE"
        )
        assert admin.execute(posture.SAFE_ROLE_FAMILY_QUERY, params).fetchone() == (
            False,
        )
        admin.execute("REVOKE clinic_resolver FROM clinic_app")
        assert admin.execute(posture.SAFE_ROLE_FAMILY_QUERY, params).fetchone() == (
            True,
        )


@contextmanager
def _secondary() -> Iterator[tuple[psycopg.Connection, str, sql.Identifier]]:
    name = "posture secondary " + uuid4().hex
    identifier = sql.Identifier(name)
    with psycopg.connect(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], autocommit=True
    ) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(identifier)
        )
        try:
            yield admin, name, identifier
        finally:
            admin.execute(sql.SQL("DROP DATABASE {}").format(identifier))


def _runtime_posture() -> bool:
    app = os.environ["APP_DATABASE_URL"]
    return posture._verify_database_posture(
        app,
        os.environ.get("TEST_DATABASE_NAME", f"test_{conninfo_to_dict(app)['dbname']}"),
    )


def test_connectable_secondary_has_independent_routine_grants() -> None:
    # The gate must discover this database: it is neither the app database
    # nor the precreated test database, and CONNECT is initially via PUBLIC.
    with (
        _secondary() as (admin, name, identifier),
        psycopg.connect(
            os.environ["TEST_SUPERUSER_DATABASE_URL"], dbname=name, autocommit=True
        ) as secondary,
    ):
        assert _runtime_posture()
        secondary.execute(
            "GRANT EXECUTE ON FUNCTION pg_catalog.pg_stat_file(text) TO clinic_app"
        )
        try:
            assert not _runtime_posture()
            admin.execute(
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(identifier)
            )
            assert _runtime_posture()
            admin.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO clinic_app").format(identifier)
            )
            assert not _runtime_posture()
        finally:
            secondary.execute(
                "REVOKE EXECUTE ON FUNCTION pg_catalog.pg_stat_file(text) "
                "FROM clinic_app"
            )
        assert _runtime_posture()


def test_connectable_but_uninspectable_database_refuses_posture() -> None:
    with _secondary() as (admin, _name, identifier):
        assert _runtime_posture()
        admin.execute(
            sql.SQL("ALTER DATABASE {} CONNECTION LIMIT 0").format(identifier)
        )
        try:
            assert not _runtime_posture()
        finally:
            admin.execute(
                sql.SQL("ALTER DATABASE {} CONNECTION LIMIT -1").format(identifier)
            )
        assert _runtime_posture()
