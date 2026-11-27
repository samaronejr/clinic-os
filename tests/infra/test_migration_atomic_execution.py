"""Rehearse the admitted atomic publications against live predecessor clones."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

import psycopg
import pytest
from django.db import migrations, models
from django.db.migrations.loader import MigrationLoader
from psycopg import sql

from database_urls import database_url_for_name
from infra.atomic_scope_observation import Publication, laws_broken
from infra.test_migration_atomic_scope import (
    ATOMIC_PINS,
    assert_atomic_admission,
    operation_digest,
)
from infra.test_protected_migration_resume import _clone, plan

if TYPE_CHECKING:
    from apps.tenancy.migrations._resumable_schema import MigrationKey

    from infra.test_protected_migration_resume import Plan

BILLING = ("billing", "0013_fiscal_documents")
PRESCRIPTION = ("prescription", "0015_sncr_issuance")
predecessor_plan = plan
PUBLICATIONS = {
    BILLING: Publication(
        frozenset(
            ("clinic_app", f"billing_{name}")
            for name in (
                "fiscalconfiguration",
                "fiscaldocument",
                "fiscaltransition",
            )
        ),
        frozenset(
            {
                ("clinic_app", 'billing_fiscal_access("uuid")'),
                ("clinic_app", "billing_fiscal_guard()"),
                ("clinic_app", "billing_fiscal_snapshot()"),
            }
        ),
    ),
    PRESCRIPTION: Publication(
        frozenset(
            ("clinic_app", f"prescription_{name}")
            for name in (
                "sncrnumberallocation",
                "prescriptionissuance",
            )
        ),
        frozenset({("clinic_app", "prescription_issuance_guard()")}),
    ),
}


@pytest.mark.parametrize("key", PUBLICATIONS, ids=".".join)
def test_atomic_lane_migration_obeys_publication_laws(
    key: MigrationKey,
    predecessor_plan: Plan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert PUBLICATIONS.keys() == ATOMIC_PINS.keys()
    with _clone(predecessor_plan.admin_url, predecessor_plan.source[key]) as db:
        assert not laws_broken(
            db,
            key,
            PUBLICATIONS[key],
            monkeypatch,
            admin_url=predecessor_plan.admin_url,
        )


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        pytest.param(
            "ALTER TABLE clinic_app.ehr_discarded_content ADD COLUMN f6_probe integer",
            {"L1", "L3"},
            id="sql-created-table",
        ),
        pytest.param(
            "ALTER TABLE clinic_app.identity_user ADD COLUMN f6_probe integer",
            {"L1", "L3"},
            id="modeled-table",
        ),
        pytest.param(
            "COMMENT ON COLUMN clinic_app.identity_user.username IS 'F6 scope probe'",
            {"L1", "L3", "L4"},
            id="column-comment",
        ),
        pytest.param(
            "GRANT EXECUTE ON FUNCTION clinic_app.f6_probe_guard() TO PUBLIC",
            {"L4"},
            id="old-routine-acl",
        ),
        pytest.param(
            "CREATE TRIGGER f6_probe BEFORE UPDATE ON clinic_app.identity_user "
            "FOR EACH ROW EXECUTE FUNCTION clinic_app.f6_probe_guard()",
            {"L1"},
            id="old-trigger",
        ),
        pytest.param(
            "CREATE INDEX f6_probe ON clinic_app.identity_user (username)",
            {"L1", "L3"},
            id="old-index",
        ),
        pytest.param(
            "CREATE POLICY f6_probe ON clinic_app.identity_user USING (false)",
            {"L1", "L3"},
            id="old-policy",
        ),
        pytest.param(
            "WITH changed AS (UPDATE clinic_app.identity_user "
            "SET username = username WHERE username = 'f6-no-such-user' RETURNING id) "
            "SELECT count(*) FROM changed",
            {"L3"},
            id="conditional-old-dml",
        ),
        pytest.param(
            "SELECT nextval("
            "pg_get_serial_sequence('clinic_app.django_migrations', 'id'))",
            {"L3", "L5"},
            id="old-sequence",
        ),
        pytest.param(
            "COMMENT ON FUNCTION clinic_app.f6_probe_guard() IS 'F6 scope probe'",
            {"L4"},
            id="old-routine-comment",
        ),
        pytest.param(
            "CREATE OR REPLACE FUNCTION clinic_app.f6_probe_guard() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN OLD; END $$",
            {"L2"},
            id="old-routine-definition",
        ),
    ],
)
def test_atomic_execution_rejects_old_effects(
    statement: str,
    expected: set[str],
    predecessor_plan: Plan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = MigrationLoader(None).disk_migrations[PRESCRIPTION]
    monkeypatch.setattr(
        type(migration),
        "operations",
        [*migration.operations, migrations.RunSQL(statement)],
    )
    with _clone(
        predecessor_plan.admin_url, predecessor_plan.source[PRESCRIPTION]
    ) as db:
        # This old routine belongs to the migration role. A permissions
        # error on an admin-owned routine would not demonstrate the oracle.
        with db.cursor() as cursor:
            cursor.execute(
                "CREATE FUNCTION clinic_app.f6_probe_guard() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$"
            )
            cursor.execute(
                "REVOKE EXECUTE ON FUNCTION clinic_app.f6_probe_guard() FROM PUBLIC"
            )
        broken = laws_broken(
            db,
            PRESCRIPTION,
            PUBLICATIONS[PRESCRIPTION],
            monkeypatch,
            admin_url=predecessor_plan.admin_url,
        )
    assert broken == expected


def test_atomic_execution_allows_new_table_referencing_old_table(
    predecessor_plan: Plan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = MigrationLoader(None).disk_migrations[PRESCRIPTION]
    monkeypatch.setattr(
        type(migration),
        "operations",
        [
            migrations.RunSQL(
                "CREATE TABLE clinic_app.f6_probe (id bigint GENERATED ALWAYS "
                "AS IDENTITY PRIMARY KEY, user_id uuid REFERENCES "
                "clinic_app.identity_user(id)); "
                "INSERT INTO clinic_app.f6_probe DEFAULT VALUES"
            )
        ],
    )
    publication = Publication(frozenset({("clinic_app", "f6_probe")}))
    with _clone(
        predecessor_plan.admin_url, predecessor_plan.source[PRESCRIPTION]
    ) as db:
        assert not laws_broken(
            db,
            PRESCRIPTION,
            publication,
            monkeypatch,
            admin_url=predecessor_plan.admin_url,
        )
        url = database_url_for_name(
            predecessor_plan.admin_url, str(db.settings_dict["NAME"])
        )
        with psycopg.connect(url) as observer:
            assert observer.execute(
                "SELECT last_value, is_called FROM clinic_app.f6_probe_id_seq"
            ).fetchone() == (1, True)


def test_atomic_execution_rejects_nested_django_operation(
    predecessor_plan: Plan, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration = MigrationLoader(None).disk_migrations[PRESCRIPTION]
    alteration = migrations.AddField(
        "prescriptiondocument", "f6_probe", models.IntegerField(null=True)
    )
    nested = migrations.SeparateDatabaseAndState(
        database_operations=[
            migrations.SeparateDatabaseAndState(database_operations=[alteration])
        ]
    )
    monkeypatch.setattr(type(migration), "operations", [*migration.operations, nested])
    with _clone(
        predecessor_plan.admin_url, predecessor_plan.source[PRESCRIPTION]
    ) as db:
        broken = laws_broken(
            db,
            PRESCRIPTION,
            PUBLICATIONS[PRESCRIPTION],
            monkeypatch,
            admin_url=predecessor_plan.admin_url,
        )
    assert broken == {"L1", "L3"}


_CHANGE_OLD_ROW = (
    "UPDATE clinic_app.django_content_type SET model = 'after' "
    "WHERE app_label = 'f6_probe' AND model = 'before'"
)


def _at_commit(statement: str) -> str:
    return (
        sql.SQL("""
        CREATE FUNCTION clinic_app.prescription_issuance_guard() RETURNS trigger
        LANGUAGE plpgsql AS $effect$ BEGIN
            {effect};
            RETURN NEW;
        END $effect$;
        CREATE CONSTRAINT TRIGGER f6_commit_effect
        AFTER INSERT ON clinic_app.prescription_prescriptionissuance
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION clinic_app.prescription_issuance_guard();
        INSERT INTO clinic_app.prescription_prescriptionissuance VALUES (1);
    """)
        .format(effect=sql.SQL(statement))
        .as_string()
    )


_PROBE_PUBLICATION = """
    CREATE TABLE clinic_app.prescription_sncrnumberallocation (id int);
    CREATE TABLE clinic_app.prescription_prescriptionissuance (id int);
"""


@pytest.mark.parametrize(
    ("statement", "expected", "committed"),
    [
        pytest.param(
            _at_commit(_CHANGE_OLD_ROW),
            {"L5"},
            (["after"], (1, True)),
            id="deferred-update",
        ),
        pytest.param(
            _at_commit("PERFORM nextval('clinic_app.django_content_type_id_seq')"),
            {"L5"},
            (["before"], (2, True)),
            id="deferred-nextval",
        ),
        pytest.param(
            _CHANGE_OLD_ROW, {"L3", "L5"}, (["after"], (1, True)), id="immediate-update"
        ),
        pytest.param(
            "INSERT INTO clinic_app.django_content_type (id, app_label, model) "
            "VALUES (2, 'f6_probe', 'added')",
            {"L3", "L5"},
            (["added", "before"], (1, True)),
            id="insert-old-row",
        ),
        pytest.param(
            "SELECT setval('clinic_app.django_content_type_id_seq', 1, false)",
            {"L3", "L5"},
            (["before"], (1, False)),
            id="setval-old-sequence",
        ),
    ],
)
def test_atomic_execution_rejects_committed_old_data_changes(
    statement: str,
    expected: set[str],
    committed: tuple[list[str], tuple[int, bool]],
    predecessor_plan: Plan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = MigrationLoader(None).disk_migrations[PRESCRIPTION]
    # Use the admitted table and helper names, not an extra object that would
    # independently trip the schema laws in the simulated re-pinning review.
    publication = PUBLICATIONS[PRESCRIPTION]
    statement_prefix = ""
    if "CREATE FUNCTION" not in statement:
        statement_prefix = """
            CREATE FUNCTION clinic_app.prescription_issuance_guard()
            RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$;
        """
    monkeypatch.setattr(
        type(migration),
        "operations",
        [migrations.RunSQL(_PROBE_PUBLICATION + statement_prefix + statement)],
    )
    with _clone(
        predecessor_plan.admin_url, predecessor_plan.source[PRESCRIPTION]
    ) as db:
        with db.cursor() as cursor:
            cursor.execute(
                "INSERT INTO clinic_app.django_content_type (app_label, model) "
                "VALUES ('f6_probe', 'before')"
            )
        url = database_url_for_name(
            predecessor_plan.admin_url, str(db.settings_dict["NAME"])
        )
        with psycopg.connect(url) as observer:
            assert observer.execute(
                "SELECT last_value, is_called "
                "FROM clinic_app.django_content_type_id_seq"
            ).fetchone() == (1, True)
        broken = laws_broken(
            db,
            PRESCRIPTION,
            publication,
            monkeypatch,
            admin_url=predecessor_plan.admin_url,
        )
        with psycopg.connect(url) as observer:
            rows = observer.execute(
                "SELECT model FROM clinic_app.django_content_type "
                "WHERE app_label = 'f6_probe' ORDER BY model"
            ).fetchall()
            assert rows == [(model,) for model in committed[0]]
            assert (
                observer.execute(
                    "SELECT last_value, is_called "
                    "FROM clinic_app.django_content_type_id_seq"
                ).fetchone()
                == committed[1]
            )
        assert broken == expected


type ProbeRow = tuple[int | None, str | None, str | None, str | None, str | None]

_ROW = (1, "stable", "before", "outer", "inner")
_NULL_ITEM = (1, None, "before", "outer", "inner")
_ALL_NULL = (None, None, None, None, None)
_OTHER_ROW = (2, "other", None, None, None)
_OTHER_HASH = (1, None, "before", "outer", "changed")


@pytest.mark.parametrize(
    ("statement", "before", "after"),
    [
        pytest.param(
            "UPDATE f6_digest.old_rows SET payload = 'after'",
            [_ROW],
            [(1, "stable", "after", "outer", "inner")],
            id="item-collision-update",
        ),
        pytest.param(
            "DELETE FROM f6_digest.old_rows WHERE item IS NULL",
            [_NULL_ITEM],
            [],
            id="null-item-delete",
        ),
        pytest.param(
            "INSERT INTO f6_digest.old_rows SELECT * FROM f6_digest.old_rows",
            [_NULL_ITEM],
            [_NULL_ITEM, _NULL_ITEM],
            id="duplicate-null-item-row",
        ),
        pytest.param(
            "UPDATE f6_digest.old_rows SET payload = ''",
            [_OTHER_ROW],
            [(2, "other", "", None, None)],
            id="null-to-empty",
        ),
        pytest.param(
            "UPDATE f6_digest.old_rows SET h = 'changed'",
            [_ROW],
            [(1, "stable", "before", "changed", "inner")],
            id="outer-alias-column",
        ),
        pytest.param(
            "UPDATE f6_digest.old_rows SET hash = 'changed' "
            "WHERE ctid = (SELECT ctid FROM f6_digest.old_rows "
            "WHERE hash = 'inner' ORDER BY ctid LIMIT 1)",
            [_NULL_ITEM, _NULL_ITEM, _OTHER_HASH],
            [_NULL_ITEM, _OTHER_HASH, _OTHER_HASH],
            id="aggregate-alias-multiplicity",
        ),
        pytest.param(
            "DELETE FROM f6_digest.old_rows",
            [_ALL_NULL],
            [],
            id="all-null-delete",
        ),
        pytest.param(
            "WITH removed AS (DELETE FROM f6_digest.old_rows RETURNING *) "
            "INSERT INTO f6_digest.old_rows SELECT * FROM removed ORDER BY id DESC",
            [_ROW, _OTHER_ROW],
            [_OTHER_ROW, _ROW],
            id="row-order-only",
        ),
    ],
)
def test_atomic_execution_compares_whole_row_multisets(
    statement: str,
    before: list[ProbeRow],
    after: list[ProbeRow],
    predecessor_plan: Plan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = MigrationLoader(None).disk_migrations[PRESCRIPTION]
    monkeypatch.setattr(
        type(migration),
        "operations",
        [migrations.RunSQL(_PROBE_PUBLICATION + _at_commit(statement))],
    )
    loader = MigrationLoader(None)
    with pytest.raises(AssertionError, match="atomic F6 operations changed"):
        assert_atomic_admission(loader)
    # Prospective re-pin only for this test: the deferred write runs after L3.
    pin = operation_digest(loader.disk_migrations[PRESCRIPTION])
    monkeypatch.setitem(ATOMIC_PINS, PRESCRIPTION, pin)
    assert_atomic_admission(loader)
    changed = Counter(before) != Counter(after)
    with _clone(
        predecessor_plan.admin_url, predecessor_plan.source[PRESCRIPTION]
    ) as db:
        with db.cursor() as cursor:
            cursor.execute("CREATE SCHEMA f6_digest")
            cursor.execute(
                "CREATE UNLOGGED TABLE f6_digest.old_rows "
                "(id integer, item text, payload text, h text, hash text)"
            )
            cursor.executemany(
                "INSERT INTO f6_digest.old_rows VALUES (%s, %s, %s, %s, %s)", before
            )
        url = database_url_for_name(
            predecessor_plan.admin_url, str(db.settings_dict["NAME"])
        )
        readback = (
            "SELECT id, item, payload, h, hash FROM f6_digest.old_rows ORDER BY ctid"
        )
        with psycopg.connect(url) as observer:
            assert observer.execute(readback).fetchall() == before
        broken = laws_broken(
            db,
            PRESCRIPTION,
            PUBLICATIONS[PRESCRIPTION],
            monkeypatch,
            admin_url=predecessor_plan.admin_url,
        )
        with psycopg.connect(url) as observer:
            committed = observer.execute(readback).fetchall()
            if changed:
                assert Counter(committed) == Counter(after)
            else:
                # The no-change control changed physical row order, not contents.
                assert committed == after
    assert broken == ({"L5"} if changed else set())
