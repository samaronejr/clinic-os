"""Distinguish a materialized PostgreSQL fast default from actual schema drift."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.tenancy.migrations import _default_witness
from apps.tenancy.migrations._resume_catalog import (
    Checkpoint,
    ResumeCatalogError,
    match_boundary,
    snapshot,
)
from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.db.migrations.executor import MigrationExecutor
from psycopg import sql

from infra.test_migration_relationship_scope import _predecessor
from infra.test_protected_migration_resume import SimulatedInterruptionError
from patients.test_intake_migrations import _default_connection, _scratch_database

if TYPE_CHECKING:
    from django.db.backends.postgresql.base import DatabaseWrapper
    from django.db.migrations import Migration
    from pytest_django.plugin import DjangoDbBlocker


def _devices(db: DatabaseWrapper) -> list[tuple[object, ...]]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT created_at, throttling_failure_count FROM otp_static_staticdevice "
            "WHERE NOT confirmed ORDER BY id"
        )
        return list(cur.fetchall())


def test_identity_retry_after_fast_defaults_materialize(
    django_db_blocker: DjangoDbBlocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = ("identity", "0019_identity_lifecycle")
    with (
        django_db_blocker.unblock(),
        _scratch_database(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as db,
        _default_connection(db),
    ):
        early = _predecessor(db, ("otp_static", "0002_throttling"))
        user = early.apps.get_model("identity", "User").objects.create(
            id=uuid4(), username="synthetic-fast-default", password=make_password(None)
        )
        early.apps.get_model("otp_static", "StaticDevice").objects.create(
            user_id=user.pk, name="Before defaults", confirmed=False
        )
        state = _predecessor(db, target)
        state.apps.get_model("otp_static", "StaticDevice").objects.create(
            user_id=user.pk,
            name="After defaults",
            confirmed=False,
            throttling_failure_count=4,
        )
        executor = MigrationExecutor(db)

        def interrupt(migration: Migration) -> None:
            assert (migration.app_label, migration.name) == target
            assert db.get_autocommit()
            assert not db.in_atomic_block
            raise SimulatedInterruptionError(migration.name)

        with monkeypatch.context() as cut:
            cut.setattr(executor, "record_migration", interrupt)
            with pytest.raises(SimulatedInterruptionError, match=target[1]):
                executor.apply_migration(state, executor.loader.get_migration(*target))
        db.close()
        assert target not in MigrationExecutor(db).loader.applied_migrations
        rows = _devices(db)
        assert [count for stamp, count in rows if stamp is not None] == [0, 4]
        pair_query = """
            SELECT attname, atthasmissing, attmissingval IS NULL FROM pg_attribute
            WHERE attrelid='otp_static_staticdevice'::regclass
              AND attname IN ('created_at', 'throttling_failure_count') ORDER BY attname
        """
        with db.cursor() as cur:
            cur.execute(pair_query)
            cols = ["created_at", "throttling_failure_count"]
            assert cur.fetchall() == [(c, True, False) for c in cols]
            cur.execute("VACUUM FULL clinic_app.otp_static_staticdevice")
            cur.execute(pair_query)
            assert cur.fetchall() == [(c, False, True) for c in cols]
        assert _devices(db) == rows
        db.close()
        MigrationExecutor(db).migrate([target])
        db.close()
        assert target in MigrationExecutor(db).loader.applied_migrations
        assert _devices(db) == rows


@pytest.mark.parametrize(
    ("case", "attribute"),
    [
        ("materialized", ""),
        ("changed-value", "attmissingval"),
        ("changed-value-type", "attmissingval"),
        ("changed-column-type", "atttypid"),
        ("reverse", "atthasmissing"),
        ("declared-default", r"default\.adbin"),
    ],
)
def test_non_otp_fast_default_transition(
    case: str, attribute: str, django_db_blocker: DjangoDbBlocker
) -> None:
    name = "relation rewrite_probe"
    checkpoint = Checkpoint("rewrite", frozenset({name}), 1)
    with (
        django_db_blocker.unblock(),
        _scratch_database(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as db,
        _default_connection(db),
        db.cursor() as cur,
    ):
        cur.execute("CREATE TABLE rewrite_probe (id int PRIMARY KEY)")
        cur.execute("INSERT INTO rewrite_probe VALUES (1)")
        cur.execute("ALTER TABLE rewrite_probe ADD a_count integer DEFAULT 3")
        if case == "reverse":
            cur.execute("ALTER TABLE rewrite_probe ADD z jsonb")
        else:
            cur.execute("ALTER TABLE rewrite_probe ADD z jsonb DEFAULT '7'")
        cur.execute("ALTER TABLE rewrite_probe ALTER z SET DEFAULT '7'")
        cur.execute("INSERT INTO rewrite_probe VALUES (2, 11, '17')")
        expected = {name: snapshot(db)[name]}
        with transaction.atomic(using=db.alias):
            _default_witness.lock_scope(db, checkpoint.scope)
            _default_witness.capture(db, checkpoint.migration, checkpoint.scope)
        cur.execute(
            "SELECT to_jsonb(attmissingval)::text FROM pg_attribute "
            "WHERE attrelid='rewrite_probe'::regclass AND attname='z'"
        )
        original_payload = cur.fetchone()[0]
        cur.execute("SELECT id, a_count, z::text FROM rewrite_probe ORDER BY id")
        rows = cur.fetchall()
        assert [r[1] for r in rows] == [3, 11]
        cur.execute("VACUUM FULL clinic_app.rewrite_probe")
        cur.execute("SELECT id, a_count, z::text FROM rewrite_probe ORDER BY id")
        assert cur.fetchall() == rows
        if case == "declared-default":
            cur.execute("ALTER TABLE rewrite_probe ALTER z SET DEFAULT '9'")
        elif case != "materialized":
            # Re-add the last column without changing visible order or the
            # declared default. The retained fast default is independent of both.
            value = {"changed-value": 9, "changed-value-type": "7"}.get(case, 7)
            kind = (
                sql.SQL("json") if case == "changed-column-type" else sql.SQL("jsonb")
            )
            cur.execute("ALTER TABLE rewrite_probe DROP COLUMN z")
            cur.execute(
                sql.SQL("ALTER TABLE rewrite_probe ADD z {} DEFAULT {}").format(
                    kind, sql.Literal(json.dumps(value))
                )
            )
            cur.execute("ALTER TABLE rewrite_probe ALTER z SET DEFAULT '7'")
        live = {name: snapshot(db)[name]}
        cur.execute(
            "SELECT key_count, encode(side_value,'hex') "
            "FROM migration_default_witness WHERE migration='rewrite' "
            "AND relation_name='rewrite_probe' AND column_name='a_count'"
        )
        cohort, side = cur.fetchone()
        assert cohort == 1
        assert expected[name]["columns.a_count.attmissingval"] == json.dumps(side)
        assert live[name]["columns.a_count.attmissingval"] == "null"
        if case == "materialized":
            with transaction.atomic(using=db.alias):
                assert match_boundary(checkpoint, live, [(1, expected)], db=db)
        else:
            assert expected[name]["columns.z.atthasmissing"] == (
                "false" if case == "reverse" else "true"
            )
            assert live[name]["columns.z.atthasmissing"] == (
                "false" if case == "declared-default" else "true"
            )
            if case == "changed-column-type":
                # The SQL values still have the same JSON projection, even
                # though the wire values and PostgreSQL types now differ.
                cur.execute(
                    "SELECT to_jsonb(attmissingval)::text FROM pg_attribute "
                    "WHERE attrelid='rewrite_probe'::regclass AND attname='z'"
                )
                assert cur.fetchone()[0] == original_payload
            with pytest.raises(
                ResumeCatalogError, match=rf"rewrite_probe\.z\.{attribute} "
            ):
                match_boundary(checkpoint, live, [(1, expected)])
