"""Rehearse the admitted atomic publications against live predecessor clones."""

from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING

import pytest
from django.db import migrations, models
from django.db.migrations.loader import MigrationLoader

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
                ("clinic_app", "billing_fiscal_access(uuid)"),
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
        assert not laws_broken(db, key, PUBLICATIONS[key], monkeypatch)


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
            {"L3"},
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
    migration = copy(MigrationLoader(None).disk_migrations[PRESCRIPTION])
    monkeypatch.setattr(
        migration, "operations", [*migration.operations, migrations.RunSQL(statement)]
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
            db, PRESCRIPTION, PUBLICATIONS[PRESCRIPTION], monkeypatch, migration
        )
    assert broken == expected


def test_atomic_execution_allows_new_table_referencing_old_table(
    predecessor_plan: Plan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = copy(MigrationLoader(None).disk_migrations[PRESCRIPTION])
    monkeypatch.setattr(
        migration,
        "operations",
        [
            migrations.RunSQL(
                "CREATE TABLE clinic_app.f6_probe (user_id uuid "
                "REFERENCES clinic_app.identity_user(id))"
            )
        ],
    )
    publication = Publication(frozenset({("clinic_app", "f6_probe")}))
    with _clone(
        predecessor_plan.admin_url, predecessor_plan.source[PRESCRIPTION]
    ) as db:
        assert not laws_broken(db, PRESCRIPTION, publication, monkeypatch, migration)


def test_atomic_execution_rejects_nested_django_operation(
    predecessor_plan: Plan, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration = copy(MigrationLoader(None).disk_migrations[PRESCRIPTION])
    alteration = migrations.AddField(
        "prescriptiondocument", "f6_probe", models.IntegerField(null=True)
    )
    nested = migrations.SeparateDatabaseAndState(
        database_operations=[
            migrations.SeparateDatabaseAndState(database_operations=[alteration])
        ]
    )
    monkeypatch.setattr(migration, "operations", [*migration.operations, nested])
    with _clone(
        predecessor_plan.admin_url, predecessor_plan.source[PRESCRIPTION]
    ) as db:
        broken = laws_broken(
            db, PRESCRIPTION, PUBLICATIONS[PRESCRIPTION], monkeypatch, migration
        )
    assert broken == {"L1", "L3"}
