from importlib import import_module
from importlib.util import find_spec
from uuid import UUID

import psycopg
import pytest
from apps.identity.models import Clinic, Organization
from django.db import connection
from psycopg import sql
from psycopg.errors import (
    CheckViolation,
    ForeignKeyViolation,
    InsufficientPrivilege,
    InvalidTextRepresentation,
)

from scheduling.availability_test_support import (
    CLINIC_A_ID,
    END,
    ORG_ID,
    PRACTITIONER_A_ID,
    START,
    seed,
    set_tenant,
)

ORG_B_ID = UUID(int=6001)
CLINIC_FOREIGN_ID = UUID(int=6101)
BLOCK_A_ID = UUID(int=6201)
BLOCK_B_ID = UUID(int=6202)


def _require_rls() -> None:
    assert find_spec("apps.scheduling.rls") is not None
    module = import_module("apps.scheduling.rls")
    expected = frozenset({("scheduling_availabilityblock", "organization_id")})
    assert expected == module.SCHEDULING_RLS_TARGETS


def _set_local(raw_connection: psycopg.Connection, organization_id: UUID) -> None:
    raw_connection.execute(
        "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
        (str(organization_id),),
    )


@pytest.mark.django_db(transaction=True)
def test_availability_has_exact_force_rls_and_runtime_acl_catalog() -> None:
    _require_rls()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity, relowner::regrole::text "
            "FROM pg_catalog.pg_class WHERE oid = "
            "'clinic_app.scheduling_availabilityblock'::regclass"
        )
        assert cursor.fetchone() == (True, True, "clinic_owner")
        cursor.execute(
            "SELECT policyname, permissive, roles, cmd, qual, with_check "
            "FROM pg_catalog.pg_policies WHERE schemaname = 'clinic_app' "
            "AND tablename = 'scheduling_availabilityblock'"
        )
        policy = cursor.fetchone()
        expected = (
            "(organization_id = (NULLIF(current_setting("
            "'app.current_tenant'::text, true), ''::text))::uuid)"
        )
        assert policy == (
            "tenant_isolation",
            "PERMISSIVE",
            ["public"],
            "ALL",
            expected,
            expected,
        )
        cursor.execute(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee = 'clinic_app' AND table_schema = 'clinic_app' "
            "AND table_name = 'scheduling_availabilityblock' "
            "ORDER BY privilege_type"
        )
        assert cursor.fetchall() == [("INSERT",), ("SELECT",)]
        cursor.execute(
            "SELECT column_name FROM information_schema.role_column_grants "
            "WHERE grantee = 'clinic_app' AND table_schema = 'clinic_app' "
            "AND table_name = 'scheduling_availabilityblock' "
            "AND privilege_type = 'UPDATE' ORDER BY column_name"
        )
        assert cursor.fetchall() == [("retired_at",), ("updated_at",)]
        cursor.execute(
            "SELECT grantee, privilege_type FROM information_schema.table_privileges "
            "WHERE table_schema = 'clinic_app' "
            "AND table_name = 'scheduling_availabilityblock' "
            "AND grantee = ANY(%s) ORDER BY grantee, privilege_type",
            [["PUBLIC", "clinic_resolver"]],
        )
        assert cursor.fetchall() == []


@pytest.mark.django_db(transaction=True)
def test_availability_runtime_is_fail_closed_and_retirement_only(
    app_database_url: str,
) -> None:
    _require_rls()
    models = import_module("apps.scheduling.models")
    availability = models.AvailabilityBlock
    organization_a, clinic_a, _, practitioner_a, practitioner_b = seed()
    set_tenant(ORG_B_ID)
    organization_b = Organization.objects.create(
        id=ORG_B_ID,
        name="Synthetic Foreign Scheduling Organization",
        cnpj="00000000006001",
    )
    clinic_foreign = Clinic.objects.create(
        id=CLINIC_FOREIGN_ID,
        organization=organization_b,
        name="Synthetic Foreign Scheduling Clinic",
        crm_uf="SP",
        timezone="America/Sao_Paulo",
    )
    availability.objects.create(
        id=BLOCK_B_ID,
        organization=organization_b,
        clinic=clinic_foreign,
        practitioner=practitioner_b,
        start_at=START,
        end_at=END,
        idempotency_key=UUID(int=6302),
        create_fingerprint=b"b" * 32,
    )
    set_tenant(ORG_ID)
    with psycopg.connect(app_database_url) as app_connection:
        _set_local(app_connection, ORG_ID)
        app_connection.execute(
            "INSERT INTO clinic_app.scheduling_availabilityblock "
            "(id, organization_id, clinic_id, practitioner_id, start_at, end_at, "
            "idempotency_key, create_fingerprint, created_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
            (
                BLOCK_A_ID,
                ORG_ID,
                CLINIC_A_ID,
                PRACTITIONER_A_ID,
                START,
                END,
                UUID(int=6301),
                b"a" * 32,
            ),
        )
        app_connection.commit()
        _set_local(app_connection, ORG_ID)
        assert app_connection.execute(
            "SELECT id FROM clinic_app.scheduling_availabilityblock ORDER BY id"
        ).fetchall() == [(BLOCK_A_ID,)]
        assert (
            app_connection.execute(
                "SELECT id FROM clinic_app.scheduling_availabilityblock "
                "WHERE clinic_id = %s",
                (CLINIC_FOREIGN_ID,),
            ).fetchall()
            == []
        )
        with pytest.raises(InsufficientPrivilege):
            app_connection.execute(
                "INSERT INTO clinic_app.scheduling_availabilityblock "
                "(id, organization_id, clinic_id, practitioner_id, start_at, end_at, "
                "idempotency_key, create_fingerprint, created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (
                    UUID(int=6401),
                    ORG_B_ID,
                    CLINIC_FOREIGN_ID,
                    practitioner_b.pk,
                    END,
                    END.replace(hour=16),
                    UUID(int=6402),
                    b"c" * 32,
                ),
            )
        app_connection.rollback()
        _set_local(app_connection, ORG_ID)
        with pytest.raises(ForeignKeyViolation):
            app_connection.execute(
                "INSERT INTO clinic_app.scheduling_availabilityblock "
                "(id, organization_id, clinic_id, practitioner_id, start_at, end_at, "
                "idempotency_key, create_fingerprint, created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (
                    UUID(int=6403),
                    ORG_ID,
                    CLINIC_FOREIGN_ID,
                    practitioner_a.pk,
                    END,
                    END.replace(hour=16),
                    UUID(int=6404),
                    b"d" * 32,
                ),
            )
        app_connection.rollback()
        _set_local(app_connection, ORG_ID)
        app_connection.execute(
            "UPDATE clinic_app.scheduling_availabilityblock "
            "SET retired_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = %s",
            (BLOCK_A_ID,),
        )
        app_connection.commit()
        for column in (
            "organization_id",
            "clinic_id",
            "practitioner_id",
            "start_at",
            "end_at",
            "idempotency_key",
            "create_fingerprint",
        ):
            _set_local(app_connection, ORG_ID)
            statement = sql.SQL(
                "UPDATE clinic_app.scheduling_availabilityblock "
                "SET {column} = {column} WHERE id = %s"
            ).format(column=sql.Identifier(column))
            with pytest.raises(InsufficientPrivilege):
                app_connection.execute(statement, (BLOCK_A_ID,))
            app_connection.rollback()
        _set_local(app_connection, ORG_ID)
        with pytest.raises(CheckViolation):
            app_connection.execute(
                "UPDATE clinic_app.scheduling_availabilityblock "
                "SET retired_at = '2020-01-01T00:00:00Z' WHERE id = %s",
                (BLOCK_A_ID,),
            )
        app_connection.rollback()
        _set_local(app_connection, ORG_ID)
        with pytest.raises(InsufficientPrivilege):
            app_connection.execute(
                "DELETE FROM clinic_app.scheduling_availabilityblock WHERE id = %s",
                (BLOCK_A_ID,),
            )
        app_connection.rollback()
    with psycopg.connect(app_database_url) as app_connection:
        assert app_connection.execute(
            "SELECT count(*) FROM clinic_app.scheduling_availabilityblock"
        ).fetchone() == (0,)
        app_connection.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', 'not-a-uuid', true)"
        )
        with pytest.raises(InvalidTextRepresentation):
            app_connection.execute(
                "SELECT count(*) FROM clinic_app.scheduling_availabilityblock"
            )
    set_tenant(ORG_ID)
    assert clinic_a.organization_id == organization_a.pk
