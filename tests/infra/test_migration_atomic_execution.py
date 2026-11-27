"""Rehearse the admitted atomic publications against live predecessor clones."""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg
import pytest
from django.db import migrations, models
from django.db.migrations.loader import MigrationLoader
from psycopg import sql

from database_urls import database_url_for_name
from infra.atomic_scope_observation import Publication, laws_broken
from infra.test_migration_atomic_scope import ATOMIC_PINS
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
    setup = """
        CREATE TABLE clinic_app.prescription_sncrnumberallocation (id int);
        CREATE TABLE clinic_app.prescription_prescriptionissuance (id int);
    """
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
        [migrations.RunSQL(setup + statement_prefix + statement)],
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
