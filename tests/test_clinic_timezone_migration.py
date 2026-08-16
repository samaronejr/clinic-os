from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from uuid import UUID

import pytest
from django.db import DatabaseError, connection
from django.db.migrations.executor import MigrationExecutor

MIGRATION_MODULE = "apps.identity.migrations.0005_clinic_timezone"
TODO2_MIGRATION = ("identity", "0004_current_actor_acl_and_physician_catalog")
TIMEZONE_MIGRATION = ("identity", "0005_clinic_timezone")
ORG_A = UUID(int=601)
ORG_B = UUID(int=602)
CLINIC_A = UUID(int=701)
CLINIC_B = UUID(int=702)
ACTOR_ID = UUID(int=801)


def _migrate(targets: list[tuple[str, str]]) -> None:
    MigrationExecutor(connection).migrate(targets)


def _migrate_head() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


def _set_session_context(organization_id: UUID) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE clinic_owner")
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_user_id', %s, false)",
            [str(ACTOR_ID)],
        )
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, false)",
            [str(organization_id)],
        )


def _reset_session_context() -> None:
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        cursor.execute("RESET app.current_user_id")
        cursor.execute("RESET app.current_tenant")


def _seed_foundation_clinics() -> None:
    for organization_id, clinic_id, suffix in (
        (ORG_A, CLINIC_A, "A"),
        (ORG_B, CLINIC_B, "B"),
    ):
        _set_session_context(organization_id)
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO clinic_app.identity_organization "
                "(id, name, cnpj) VALUES (%s, %s, %s)",
                [
                    organization_id,
                    f"Synthetic Organization {suffix}",
                    f"{int(organization_id):014d}",
                ],
            )
            cursor.execute(
                "INSERT INTO clinic_app.identity_clinic "
                "(id, organization_id, name, crm_uf) VALUES (%s, %s, %s, %s)",
                [clinic_id, organization_id, f"Synthetic Clinic {suffix}", "SP"],
            )


def test_timezone_migration_is_the_only_explicit_post_todo2_leaf() -> None:
    module = import_module(MIGRATION_MODULE) if find_spec(MIGRATION_MODULE) else None
    assert module is not None
    assert module.Migration.dependencies == [
        ("identity", "0004_current_actor_acl_and_physician_catalog")
    ]


def test_timezone_migration_orders_nullable_backfill_required_and_check() -> None:
    module = import_module(MIGRATION_MODULE)

    assert [type(operation).__name__ for operation in module.Migration.operations] == [
        "AddField",
        "RunPython",
        "AlterField",
        "AddConstraint",
    ]


@pytest.mark.django_db(transaction=True)
def test_two_tenant_backfill_restores_role_and_gucs() -> None:
    try:
        _migrate([TODO2_MIGRATION])
        _seed_foundation_clinics()
        _set_session_context(ORG_B)

        _migrate([TIMEZONE_MIGRATION])

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_setting('role'), "
                "current_setting('app.current_tenant', true), "
                "current_setting('app.current_user_id', true)"
            )
            assert cursor.fetchone() == (
                "clinic_owner",
                str(ORG_B),
                str(ACTOR_ID),
            )
            for organization_id, clinic_id in ((ORG_A, CLINIC_A), (ORG_B, CLINIC_B)):
                cursor.execute(
                    "SELECT pg_catalog.set_config('app.current_tenant', %s, false)",
                    [str(organization_id)],
                )
                cursor.execute(
                    "SELECT timezone FROM clinic_app.identity_clinic WHERE id = %s",
                    [clinic_id],
                )
                assert cursor.fetchone() == ("America/Sao_Paulo",)
    finally:
        _reset_session_context()
        _migrate_head()


@pytest.mark.django_db(transaction=True)
def test_failed_backfill_restores_role_and_gucs() -> None:
    try:
        _migrate([TODO2_MIGRATION])
        _seed_foundation_clinics()
        _set_session_context(ORG_B)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE FUNCTION clinic_app.todo3_fail_timezone_update()
                RETURNS trigger LANGUAGE plpgsql AS $function$
                BEGIN
                    RAISE EXCEPTION 'synthetic timezone failure'
                        USING ERRCODE = 'check_violation';
                END
                $function$
                """
            )
            cursor.execute(
                "CREATE TRIGGER todo3_fail_timezone_update "
                "BEFORE UPDATE ON clinic_app.identity_clinic "
                "FOR EACH ROW EXECUTE FUNCTION "
                "clinic_app.todo3_fail_timezone_update()"
            )

        with pytest.raises(DatabaseError, match="synthetic timezone failure"):
            _migrate([TIMEZONE_MIGRATION])

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_setting('role'), "
                "current_setting('app.current_tenant', true), "
                "current_setting('app.current_user_id', true)"
            )
            assert cursor.fetchone() == (
                "clinic_owner",
                str(ORG_B),
                str(ACTOR_ID),
            )
    finally:
        _reset_session_context()
        with connection.cursor() as cursor:
            cursor.execute(
                "DROP TRIGGER IF EXISTS todo3_fail_timezone_update "
                "ON clinic_app.identity_clinic"
            )
            cursor.execute(
                "DROP FUNCTION IF EXISTS clinic_app.todo3_fail_timezone_update()"
            )
        _migrate_head()


@pytest.mark.django_db(transaction=True)
def test_timezone_migration_reverses_reapplies_and_installs_named_check() -> None:
    try:
        _migrate([TODO2_MIGRATION])
        _seed_foundation_clinics()

        _migrate([TIMEZONE_MIGRATION])
        _migrate([TODO2_MIGRATION])
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'clinic_app' "
                "AND table_name = 'identity_clinic' AND column_name = 'timezone'"
            )
            assert cursor.fetchone() is None

        _migrate([TIMEZONE_MIGRATION])
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'clinic_app.identity_clinic'::regclass "
                "AND conname = 'identity_clinic_timezone_nonblank'"
            )
            definition = cursor.fetchone()
            assert definition is not None
            assert "btrim" in definition[0]
            assert "<> ''::text" in definition[0]
    finally:
        _reset_session_context()
        _migrate_head()


def test_timezone_migration_never_disables_rls_or_names_a_superuser() -> None:
    helper = import_module("apps.identity.phase1a_timezone_migration")
    module_path = helper.__file__
    assert isinstance(module_path, str)
    source = Path(module_path).read_text()

    assert "DISABLE ROW LEVEL SECURITY" not in source
    assert "clinic_super" not in source
    assert "SET LOCAL ROLE clinic_resolver" in source
