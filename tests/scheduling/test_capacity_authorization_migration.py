"""The authorization-only upgrade restores the exact deployed predecessor."""

from importlib import import_module

import pytest
from apps.scheduling.migrations._resources_sql import GUARDS_SQL
from django.db import connection, transaction

pytestmark = pytest.mark.django_db(transaction=True)


def test_capacity_authorization_reverse_restores_exact_body_and_posture() -> None:
    migration = import_module(
        "apps.scheduling.migrations.0006_capacity_insert_authorization"
    )
    operation = migration.Migration.operations[0]
    header = "CREATE FUNCTION clinic_app.scheduling_capacity_guard()"
    previous = GUARDS_SQL.split(header, 1)[1].split("AS $f$", 1)[1].split("$f$;", 1)[0]
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_functiondef(oid), proowner, proacl, prosecdef, proconfig "
            "FROM pg_proc WHERE oid="
            "'clinic_app.scheduling_capacity_guard()'::regprocedure"
        )
        candidate = cursor.fetchone()
        cursor.execute(operation.reverse_sql)
        cursor.execute(
            "SELECT prosrc FROM pg_proc WHERE oid="
            "'clinic_app.scheduling_capacity_guard()'::regprocedure"
        )
        reversed_body = cursor.fetchone()
        assert reversed_body == (previous,)
        cursor.execute(operation.sql)
        cursor.execute(
            "SELECT pg_get_functiondef(oid), proowner, proacl, prosecdef, proconfig "
            "FROM pg_proc WHERE oid="
            "'clinic_app.scheduling_capacity_guard()'::regprocedure"
        )
        assert cursor.fetchone() == candidate
        transaction.set_rollback(True)
