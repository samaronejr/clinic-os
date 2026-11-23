"""Exercise catalog scope using real inheritance, partitions and migration history."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest
from apps.tenancy.migrations._resume_catalog import (
    Checkpoint,
    ResumeCatalogError,
    match_boundary,
    snapshot,
)
from django.db.migrations.executor import MigrationExecutor
from psycopg import sql

from infra import test_protected_migration_resume as resume
from patients.test_intake_migrations import _default_connection, _scratch_database

if TYPE_CHECKING:
    from apps.tenancy.migrations._resume_catalog import Catalog
    from django.db.backends.postgresql.base import DatabaseWrapper
    from django.db.migrations.state import ProjectState
    from pytest_django.plugin import DjangoDbBlocker


def _predecessor(db: DatabaseWrapper, target: resume.Target) -> ProjectState:
    executor = MigrationExecutor(db)
    graph = executor.loader.graph
    # Local application branches may already be ahead of one of the parents.
    # A graph ancestor walk never asks Django to undo an unrelated branch.
    plan = [
        (graph.nodes[key], False)
        for key in graph.forwards_plan(target)
        if key != target and key not in executor.loader.applied_migrations
    ]
    return executor.migrate([target], plan=plan)


def test_resume_refuses_unexpected_inheritance_child(
    django_db_blocker: DjangoDbBlocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    with (
        django_db_blocker.unblock(),
        _scratch_database(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as db,
        _default_connection(db),
    ):
        state = _predecessor(db, resume.AI)
        read = resume._seed(db, state, resume.AI)
        protected = read(db)
        with pytest.raises(
            resume.SimulatedInterruptionError, match="transaction commit"
        ):
            resume._apply_with_cut(db, resume.AI, monkeypatch, 1)
        db.close()
        assert resume.AI not in MigrationExecutor(db).loader.applied_migrations
        with db.cursor() as cur:
            cur.execute("CREATE TABLE review_child () INHERITS (ai_aiartifact)")
            cur.execute("EXPLAIN (FORMAT JSON) SELECT * FROM ai_aiartifact")
            assert "review_child" in str(cur.fetchone())
        db.close()
        with pytest.raises(ResumeCatalogError, match=r"ai_aiartifact.*review_child"):
            MigrationExecutor(db).migrate([resume.AI])
        db.close()
        assert resume.AI not in MigrationExecutor(db).loader.applied_migrations
        with db.cursor() as cur:
            cur.execute(
                "SELECT inhrelid::regclass::text FROM pg_inherits "
                "WHERE inhparent='ai_aiartifact'::regclass"
            )
            assert cur.fetchall() == [("review_child",)]
            cur.execute("DROP TABLE review_child")
            cur.execute(
                "SELECT relhassubclass FROM pg_class "
                "WHERE oid='ai_aiartifact'::regclass"
            )
            assert cur.fetchone() == (True,)
        # Membership was removed; the stale hint alone must not prevent retry.
        MigrationExecutor(db).migrate([resume.AI])
        assert resume.AI in MigrationExecutor(db).loader.applied_migrations
        assert read(db) == protected


def test_attached_partition_changes_the_guarded_parent(
    django_db_blocker: DjangoDbBlocker,
) -> None:
    with (
        django_db_blocker.unblock(),
        _scratch_database(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as db,
        _default_connection(db),
    ):
        MigrationExecutor(db).recorder.ensure_schema()
        with db.cursor() as cur:
            cur.execute("CREATE TABLE scope_parent (n integer) PARTITION BY RANGE (n)")
            cur.execute("CREATE TABLE scope_leaf (n integer)")
        obj = "relation scope_parent"
        checkpoint = Checkpoint("partition-scope", frozenset({obj}), 1)
        clean = {obj: snapshot(db)[obj]}
        states: list[tuple[int, Catalog]] = [(0, {obj: {}}), (1, clean)]
        assert match_boundary(checkpoint, clean, states)
        with db.cursor() as cur:
            cur.execute(
                "ALTER TABLE scope_parent ATTACH PARTITION scope_leaf "
                "FOR VALUES FROM (1) TO (8)"
            )
            cur.execute(
                "SELECT relispartition, pg_get_expr(relpartbound, oid) FROM pg_class "
                "WHERE oid='scope_leaf'::regclass"
            )
            assert cur.fetchone() == (True, "FOR VALUES FROM (1) TO (8)")
        db.close()
        attached = {obj: snapshot(db)[obj]}
        with pytest.raises(ResumeCatalogError, match=r"scope_parent.*scope_leaf"):
            match_boundary(checkpoint, attached, states)
        with db.cursor() as cur:
            cur.execute("ALTER TABLE scope_parent DETACH PARTITION scope_leaf")
            cur.execute(
                "ALTER TABLE scope_parent ATTACH PARTITION scope_leaf "
                "FOR VALUES FROM (2) TO (8)"
            )
        with pytest.raises(ResumeCatalogError, match=r"scope_parent.*scope_leaf"):
            match_boundary(checkpoint, {obj: snapshot(db)[obj]}, [(1, attached)])


def test_nested_partition_shape_is_scoped_to_ancestors_and_descendants(
    django_db_blocker: DjangoDbBlocker,
) -> None:
    with (
        django_db_blocker.unblock(),
        _scratch_database(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as db,
        _default_connection(db),
    ):
        MigrationExecutor(db).recorder.ensure_schema()
        with db.cursor() as cur:
            cur.execute("""
                CREATE SCHEMA scope_external;
                CREATE TABLE scope_root(n int, k int) PARTITION BY RANGE(n);
                CREATE TABLE scope_external.branch PARTITION OF scope_root
                    FOR VALUES FROM (0) TO (10) PARTITION BY LIST(k);
                CREATE TABLE scope_leaf(n int, k int);
            """)
        root = "relation scope_root"
        clean = {root: snapshot(db)[root]}
        assert "partitioning.scope_external.branch.partstrat" in clean[root]
        with db.cursor() as cur:
            cur.execute(
                "ALTER TABLE scope_external.branch ATTACH PARTITION scope_leaf "
                "FOR VALUES IN (7)"
            )
        attached = snapshot(db)
        assert any(
            "scope_root" in key and key.startswith("parents.")
            for key in attached["relation scope_leaf"]
        )
        with pytest.raises(ResumeCatalogError, match=r"scope_root.*scope_leaf"):
            match_boundary(
                Checkpoint("nested", frozenset({root}), 1),
                {root: attached[root]},
                [(1, clean)],
            )


@pytest.mark.parametrize("cycles", [2, 3], ids=["two-cycles", "three-cycles"])
def test_repeated_timezone_reapply_preserves_logical_shape(
    cycles: int, django_db_blocker: DjangoDbBlocker
) -> None:
    timezone = ("identity", "0005_clinic_timezone")
    with (
        django_db_blocker.unblock(),
        _scratch_database(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as db,
        _default_connection(db),
    ):
        executor = MigrationExecutor(db)
        parents = sorted(
            n.key for n in executor.loader.graph.node_map[timezone].parents
        )
        executor.migrate([timezone])
        for _ in range(cycles):
            MigrationExecutor(db).migrate(parents)
            MigrationExecutor(db).migrate([timezone])
        with db.cursor() as cur:
            cur.execute(
                "SELECT attnum FROM pg_attribute WHERE attname='timezone' "
                "AND attrelid='identity_clinic'::regclass"
            )
            assert cur.fetchone() == (5 + cycles,)
            cur.execute("SELECT * FROM identity_clinic LIMIT 0")
            assert cur.description[4].name == "timezone"
        state = _predecessor(db, resume.INTAKE)
        read = resume._seed(db, state, resume.INTAKE)
        protected = read(db)
        db.close()
        assert resume.INTAKE not in MigrationExecutor(db).loader.applied_migrations
        MigrationExecutor(db).migrate([resume.INTAKE])
        assert resume.INTAKE in MigrationExecutor(db).loader.applied_migrations
        assert read(db) == protected


def test_restored_otp_statistics_target_is_catalog_equivalent(
    django_db_blocker: DjangoDbBlocker,
) -> None:
    target = ("identity", "0019_identity_lifecycle")
    with (
        django_db_blocker.unblock(),
        _scratch_database(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as db,
        _default_connection(db),
    ):
        _predecessor(db, target)
        query = (
            "SELECT to_jsonb(a)::text FROM pg_attribute a "
            "WHERE attrelid='otp_totp_totpdevice'::regclass AND attname='created_at'"
        )
        with db.cursor() as cur:
            cur.execute(query)
            before = json.loads(cur.fetchone()[0])
            assert before["attstattarget"] == -1
            assert before["atthasmissing"]
            assert before["attmissingval"]
            cur.execute(
                "ALTER TABLE otp_totp_totpdevice ALTER created_at SET STATISTICS 13"
            )
            cur.execute(query)
            assert json.loads(cur.fetchone()[0])["attstattarget"] == 13
            cur.execute(
                "ALTER TABLE otp_totp_totpdevice ALTER created_at SET STATISTICS -1"
            )
            cur.execute(query)
            assert json.loads(cur.fetchone()[0]) == before
        db.close()
        assert target not in MigrationExecutor(db).loader.applied_migrations
        MigrationExecutor(db).migrate([target])
        assert target in MigrationExecutor(db).loader.applied_migrations


def _replace_otp_literal(
    db: DatabaseWrapper, relation: sql.Identifier, mode: str
) -> None:
    definition = {
        "finite-literal": sql.SQL("timestamptz DEFAULT '2001-01-01T00:00:00Z'"),
        "text-literal": sql.SQL("text DEFAULT 'not-a-time'"),
        "infinity": sql.SQL("timestamptz DEFAULT 'infinity'"),
    }[mode]
    with db.cursor() as cur:
        cur.execute(
            sql.SQL("ALTER TABLE {} DROP last_used_at, DROP created_at").format(
                relation
            )
        )
        cur.execute(
            sql.SQL(
                "ALTER TABLE {} ADD created_at {}, ADD last_used_at timestamptz"
            ).format(relation, definition)
        )
        cur.execute(
            sql.SQL("ALTER TABLE {} ALTER created_at DROP DEFAULT").format(relation)
        )


def _change_otp_default_function(db: DatabaseWrapper, table: str) -> None:
    relation = sql.Identifier("clinic_app", table)
    key = "relation " + table
    with db.cursor() as cur:
        cur.execute(
            sql.SQL("ALTER TABLE {} ALTER created_at SET DEFAULT now()").format(
                relation
            )
        )
        first = {key: snapshot(db)[key]}
        cur.execute(
            sql.SQL(
                "ALTER TABLE {} ALTER created_at SET DEFAULT clock_timestamp()"
            ).format(relation)
        )
        second = {key: snapshot(db)[key]}
    field = "columns.created_at.default.adbin"
    assert json.loads(first[key][field]) == "now()"
    assert json.loads(second[key][field]) == "clock_timestamp()"
    with pytest.raises(ResumeCatalogError, match=r"created_at\.default\.adbin"):
        match_boundary(
            Checkpoint("function-shape", frozenset({key}), 1), second, [(1, first)]
        )


@pytest.mark.parametrize("table", ["otp_totp_totpdevice", "otp_static_staticdevice"])
@pytest.mark.parametrize(
    "mode", ["finite-literal", "function", "text-literal", "infinity"]
)
def test_otp_created_at_compares_schema_not_install_time(
    table: str, mode: str, django_db_blocker: DjangoDbBlocker
) -> None:
    target = ("identity", "0019_identity_lifecycle")
    relation = sql.Identifier("clinic_app", table)
    descriptor = """
        SELECT format_type(a.atttypid,a.atttypmod), to_jsonb(a.attmissingval)::text,
               a.atthasmissing, pg_get_expr(d.adbin,d.adrelid)
        FROM pg_attribute a LEFT JOIN pg_attrdef d
          ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE a.attrelid=to_regclass(%s) AND a.attname='created_at'
    """
    with (
        django_db_blocker.unblock(),
        _scratch_database(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as db,
        _default_connection(db),
    ):
        _predecessor(db, target)
        key = "relation " + table
        before = snapshot(db)[key]
        with db.cursor() as cur:
            cur.execute(descriptor, ["clinic_app." + table])
            kind, original, missing, expr = cur.fetchone()
            assert kind == "timestamp with time zone"
            assert missing is True
            assert expr is None
            cur.execute(sql.SQL("SELECT * FROM {} LIMIT 0").format(relation))
            order = tuple(c[0] for c in cur.description)
            if mode == "function":
                _change_otp_default_function(db, table)
            else:
                _replace_otp_literal(db, relation, mode)
            cur.execute(sql.SQL("SELECT * FROM {} LIMIT 0").format(relation))
            assert tuple(c[0] for c in cur.description) == order
            cur.execute(descriptor, ["clinic_app." + table])
            kind, actual, missing, expr = cur.fetchone()
        assert missing is True
        updated = snapshot(db)[key]
        db.close()
        assert target not in MigrationExecutor(db).loader.applied_migrations
        if mode == "finite-literal":
            assert original != actual
            assert expr is None
            assert updated == before
            MigrationExecutor(db).migrate([target])
            assert target in MigrationExecutor(db).loader.applied_migrations
            return
        if mode == "function":
            assert expr == "clock_timestamp()"
        elif mode == "text-literal":
            assert kind == "text"
            assert json.loads(actual) == ["not-a-time"]
        else:
            assert kind == "timestamp with time zone"
            assert json.loads(actual) == ["infinity"]
        with pytest.raises(ResumeCatalogError, match=table + r"\.created_at\."):
            MigrationExecutor(db).migrate([target])
        db.close()
        assert target not in MigrationExecutor(db).loader.applied_migrations
        assert snapshot(db)[key] == updated


def test_logical_shape_normalizes_references_not_just_attribute_rows(
    django_db_blocker: DjangoDbBlocker,
) -> None:
    with (
        django_db_blocker.unblock(),
        _scratch_database(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as db,
        _default_connection(db),
    ):
        MigrationExecutor(db).recorder.ensure_schema()
        prefix = "CREATE TABLE scope_keys(first integer, "
        suffix = "key integer DEFAULT 1, CONSTRAINT positive CHECK (key>0));"
        checks = (
            "CREATE INDEX scope_idx ON scope_keys(key, (first+1)) WHERE key<>2;"
            "CREATE TRIGGER scope_update BEFORE UPDATE OF key ON scope_keys "
            "FOR EACH ROW EXECUTE FUNCTION scope_guard();"
        )
        with db.cursor() as cur:
            cur.execute(
                "CREATE FUNCTION scope_guard() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN RETURN NEW; END $$"
            )
            cur.execute(prefix + suffix + checks)
        clean = snapshot(db)["relation scope_keys"]
        with db.cursor() as cur:
            cur.execute("DROP TABLE scope_keys;" + prefix + "gone integer, " + suffix)
            cur.execute("ALTER TABLE scope_keys DROP COLUMN gone;" + checks)
            cur.execute(
                "SELECT attnum FROM pg_attribute WHERE attname='key' AND "
                "attrelid='scope_keys'::regclass"
            )
            assert cur.fetchone() == (3,)
        assert snapshot(db)["relation scope_keys"] == clean
