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


@pytest.mark.usefixtures("clean_posture_environment")
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
        "ALTER ROLE {} REPLICATION",
        "GRANT pg_stat_scan_tables TO {}",
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
        "replication",
        "scan-tables",
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
        admin.execute(posture.CATALOG_PATH)
        logins = list(posture.PINNED_ROLES)
        assert posture._role_authority_safe(admin, logins)
        admin.execute(sql.SQL(grant).format(leaf))
        assert not posture._role_authority_safe(admin, logins)
        admin.execute(sql.SQL("REVOKE {} FROM clinic_app").format(bridge))
        assert posture._role_authority_safe(admin, logins)


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
        admin.execute(posture.CATALOG_PATH)
        logins = list(posture.PINNED_ROLES)
        assert posture._role_authority_safe(admin, logins)
        admin.execute(sql.SQL(grant).format(leaf))
        assert not posture._role_authority_safe(admin, logins)
        admin.execute(sql.SQL(undo).format(leaf))
        assert posture._role_authority_safe(admin, logins)


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


@pytest.mark.usefixtures("clean_posture_environment")
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
            # Give the inspector its own access; a clean result here would
            # mean that removing app CONNECT hid the committed grant.
            admin.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO clinic_posture").format(
                    identifier
                )
            )
            assert not _runtime_posture()
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


def test_native_memory_routine_direct_grant_refused() -> None:
    with (
        psycopg.connect(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as admin,
        admin.transaction(force_rollback=True),
    ):
        admin.execute(posture.CATALOG_PATH)
        logins = list(posture.PINNED_ROLES)
        assert posture._role_authority_safe(admin, logins)
        admin.execute(
            "GRANT EXECUTE ON FUNCTION pg_log_backend_memory_contexts(integer) "
            "TO clinic_app"
        )
        assert not posture._role_authority_safe(admin, logins)
        admin.execute(
            "REVOKE EXECUTE ON FUNCTION pg_log_backend_memory_contexts(integer) "
            "FROM clinic_app"
        )
        assert posture._role_authority_safe(admin, logins)


def test_unlisted_restricted_routine_in_extension_schema() -> None:
    with (
        psycopg.connect(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as admin,
        admin.transaction(force_rollback=True),
    ):
        admin.execute(posture.CATALOG_PATH)
        schema = sql.Identifier("posture_" + uuid4().hex)
        routine = sql.SQL("{}.{}()").format(schema, sql.Identifier(uuid4().hex))
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
        admin.execute(sql.SQL("CREATE EXTENSION hstore WITH SCHEMA {}").format(schema))
        # Not itself an extension member, not known by name when code shipped.
        admin.execute(
            sql.SQL("CREATE FUNCTION {} RETURNS int LANGUAGE SQL RETURN 1").format(
                routine
            )
        )
        admin.execute(sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(routine))
        bridge, leaf = _create_roles(admin)
        admin.execute(sql.SQL("GRANT {} TO {} WITH INHERIT FALSE").format(leaf, bridge))
        admin.execute(
            sql.SQL("GRANT {} TO clinic_app WITH INHERIT FALSE").format(bridge)
        )
        logins = list(posture.PINNED_ROLES)
        assert posture._role_authority_safe(admin, logins)
        admin.execute(
            sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(routine, leaf)
        )
        assert not posture._role_authority_safe(admin, logins)
        admin.execute(
            sql.SQL("REVOKE EXECUTE ON FUNCTION {} FROM {}").format(routine, leaf)
        )
        assert posture._role_authority_safe(admin, logins)


@contextmanager
def _reachable_machine(*, from_app: bool = True) -> Iterator[tuple[str, str]]:
    name = "clinic_agent_" + uuid4().hex
    machine = sql.Identifier(name)
    password = uuid4().hex
    with psycopg.connect(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], autocommit=True
    ) as admin:
        with admin.transaction():
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN NOINHERIT PASSWORD {}").format(
                    machine, sql.Literal(password)
                )
            )
            for setting, value in (
                ("log_error_verbosity", "terse"),
                ("log_parameter_max_length", "0"),
                ("log_parameter_max_length_on_error", "0"),
            ):
                admin.execute(
                    sql.SQL("ALTER ROLE {} SET {} TO {}").format(
                        machine, sql.Identifier(setting), sql.Literal(value)
                    )
                )
            if from_app:
                admin.execute(
                    sql.SQL(
                        "GRANT {} TO clinic_app WITH INHERIT FALSE, SET TRUE"
                    ).format(machine)
                )
        try:
            yield name, password
        finally:
            admin.execute(sql.SQL("DROP ROLE {}").format(machine))


@pytest.mark.usefixtures("clean_posture_environment")
def test_machine_only_database_keeps_local_grant_in_scope() -> None:
    with (
        _reachable_machine() as (machine_name, password),
        _secondary() as (admin, name, identifier),
        psycopg.connect(
            os.environ["TEST_SUPERUSER_DATABASE_URL"], dbname=name, autocommit=True
        ) as secondary,
    ):
        machine = sql.Identifier(machine_name)
        admin.execute(
            sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(identifier)
        )
        admin.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, clinic_posture").format(
                identifier, machine
            )
        )
        assert admin.execute(
            "SELECT has_database_privilege('clinic_app', %s, 'CONNECT'),"
            " pg_has_role('clinic_app', %s, 'SET')",
            (name, machine_name),
        ).fetchone() == (False, True)
        with psycopg.connect(
            os.environ["APP_DATABASE_URL"],
            dbname=name,
            user=machine_name,
            password=password,
        ) as peer:
            assert peer.execute("SELECT current_user").fetchone() == (machine_name,)
        assert _runtime_posture()
        secondary.execute(
            sql.SQL(
                "GRANT EXECUTE ON FUNCTION pg_catalog.pg_stat_file(text) TO {}"
            ).format(machine)
        )
        assert not _runtime_posture()
        secondary.execute(
            sql.SQL(
                "REVOKE EXECUTE ON FUNCTION pg_catalog.pg_stat_file(text) FROM {}"
            ).format(machine)
        )
        assert _runtime_posture()


@pytest.mark.usefixtures("clean_posture_environment")
def test_reachable_database_owner_activates_local_grant() -> None:
    with (
        _secondary() as (admin, name, identifier),
        psycopg.connect(
            os.environ["TEST_SUPERUSER_DATABASE_URL"], dbname=name, autocommit=True
        ) as secondary,
    ):
        owner_name = "posture_owner_" + uuid4().hex
        owner = sql.Identifier(owner_name)
        admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(owner))
        try:
            admin.execute(
                sql.SQL("ALTER DATABASE {} OWNER TO {}").format(identifier, owner)
            )
            admin.execute(
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(identifier)
            )
            admin.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO clinic_posture").format(
                    identifier
                )
            )
            assert _runtime_posture()
            admin.execute(
                sql.SQL("GRANT {} TO clinic_app WITH INHERIT FALSE, SET TRUE").format(
                    owner
                )
            )
            assert not _runtime_posture()  # Bare local ownership already denies.
            secondary.execute("SET search_path = pg_catalog, pg_temp")
            logins = list(posture.PINNED_ROLES)
            assert posture._routine_authority_safe(secondary, logins)
            secondary.execute(
                "GRANT EXECUTE ON FUNCTION pg_catalog.pg_stat_file(text) "
                "TO pg_database_owner"
            )
            assert secondary.execute(
                "SELECT has_function_privilege("
                "'clinic_app', 'pg_stat_file(text)', 'EXECUTE'),"
                " has_function_privilege(%s, 'pg_stat_file(text)', 'EXECUTE')",
                (owner_name,),
            ).fetchone() == (False, True)
            assert not posture._routine_authority_safe(secondary, logins)
            assert not _runtime_posture()
            secondary.execute(
                "REVOKE EXECUTE ON FUNCTION pg_catalog.pg_stat_file(text) "
                "FROM pg_database_owner"
            )
            assert posture._routine_authority_safe(secondary, logins)
            assert not _runtime_posture()
            admin.execute(sql.SQL("REVOKE {} FROM clinic_app").format(owner))
            assert _runtime_posture()
        finally:
            admin.execute(
                sql.SQL("ALTER DATABASE {} OWNER TO CURRENT_USER").format(identifier)
            )
            admin.execute(sql.SQL("DROP ROLE {}").format(owner))


@pytest.mark.usefixtures("clean_posture_environment")
@pytest.mark.parametrize("runtime", ["clinic_app", "clinic_agent"])
def test_bare_database_ownership_denied_without_delegated_routine(runtime: str) -> None:
    with _secondary() as (admin, name, identifier):
        # Isolate the bare role from its stock ownership of the public schema.
        # This is a new disposable database; the runtime gains no other object.
        with psycopg.connect(
            os.environ["TEST_SUPERUSER_DATABASE_URL"], dbname=name
        ) as peer:
            peer.execute("ALTER SCHEMA public OWNER TO CURRENT_USER")
        assert _runtime_posture()
        admin.execute(
            sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                identifier, sql.Identifier(runtime)
            )
        )
        try:
            with psycopg.connect(
                os.environ["TEST_SUPERUSER_DATABASE_URL"], dbname=name
            ) as peer:
                assert peer.execute(
                    "SELECT pg_has_role(%s,'pg_database_owner','MEMBER')", (runtime,)
                ).fetchone() == (True,)
                assert peer.execute(
                    "SELECT has_function_privilege(%s,'pg_stat_file(text)','EXECUTE')",
                    (runtime,),
                ).fetchone() == (False,)
            assert not _runtime_posture()
        finally:
            admin.execute(
                sql.SQL("ALTER DATABASE {} OWNER TO CURRENT_USER").format(identifier)
            )
        assert _runtime_posture()


@pytest.mark.usefixtures("clean_posture_environment")
def test_independent_machine_owner_is_refused() -> None:
    with (
        _reachable_machine(from_app=False) as (machine, _password),
        _secondary() as (admin, name, identifier),
    ):
        with psycopg.connect(
            os.environ["TEST_SUPERUSER_DATABASE_URL"], dbname=name
        ) as peer:
            peer.execute("ALTER SCHEMA public OWNER TO CURRENT_USER")
        assert admin.execute(
            "SELECT pg_has_role('clinic_app', %s, 'MEMBER')", (machine,)
        ).fetchone() == (False,)
        assert _runtime_posture()
        admin.execute(
            sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                identifier, sql.Identifier(machine)
            )
        )
        try:
            assert not _runtime_posture()
        finally:
            admin.execute(
                sql.SQL("ALTER DATABASE {} OWNER TO CURRENT_USER").format(identifier)
            )
        assert _runtime_posture()


@pytest.mark.usefixtures("clean_posture_environment")
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


@pytest.mark.usefixtures("clean_posture_environment")
def test_connection_disabled_database_is_not_skipped() -> None:
    with _secondary() as (admin, _name, identifier):
        assert _runtime_posture()
        admin.execute(
            sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(identifier)
        )
        try:
            assert not _runtime_posture()
        finally:
            admin.execute(
                sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS true").format(identifier)
            )
        assert _runtime_posture()


def test_initially_restricted_native_routine_cannot_be_opened_to_public() -> None:
    with (
        psycopg.connect(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as admin,
        admin.transaction(force_rollback=True),
    ):
        admin.execute(posture.CATALOG_PATH)
        logins = list(posture.PINNED_ROLES)
        assert posture._role_authority_safe(admin, logins)
        admin.execute("GRANT EXECUTE ON FUNCTION pg_ls_waldir() TO PUBLIC")
        assert not posture._role_authority_safe(admin, logins)


def test_private_migrated_helper_does_not_allow_new_runtime_grantee() -> None:
    with (
        psycopg.connect(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as admin,
        admin.transaction(force_rollback=True),
    ):
        admin.execute(posture.CATALOG_PATH)
        logins = list(posture.PINNED_ROLES)
        assert posture._role_authority_safe(admin, logins)
        admin.execute(
            "GRANT EXECUTE ON FUNCTION clinic_app.intake_patient_mirror_guard_v1() "
            "TO clinic_app"
        )
        assert not posture._role_authority_safe(admin, logins)
