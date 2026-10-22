"""The native bundle guard trusts the database login, never a staff role."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.identity.models import UserClinicRole
from django.db import connection, transaction
from psycopg import sql
from psycopg.errors import CheckViolation

from auth.stepup_test_support import create_role_actor
from database_urls import database_url_for_name

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_bundle_issuance_requires_the_owner_login_for_every_role(
    rbac_graph: RbacGraph,
) -> None:
    table = sql.Identifier("public", "workflow_issuer_" + uuid4().hex)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("CREATE TABLE {} (workflow_bundle_version smallint)").format(table)
        )
        cursor.execute("SET LOCAL ROLE clinic_resolver")
        cursor.execute(
            "GRANT EXECUTE ON FUNCTION clinic_app.workflows_bundle_selection_guard() "
            "TO clinic_owner"
        )
        cursor.execute("RESET ROLE")
        cursor.execute(
            sql.SQL(
                "CREATE TRIGGER issuer_guard BEFORE INSERT OR UPDATE ON {} "
                "FOR EACH ROW EXECUTE FUNCTION "
                "clinic_app.workflows_bundle_selection_guard()"
            ).format(table)
        )
        cursor.execute(sql.SQL("GRANT INSERT ON {} TO clinic_app").format(table))
        cursor.execute("SET LOCAL ROLE clinic_resolver")
        cursor.execute(
            "REVOKE EXECUTE ON FUNCTION clinic_app.workflows_bundle_selection_guard() "
            "FROM clinic_owner"
        )
        cursor.execute("RESET ROLE")
    try:
        app_url = database_url_for_name(
            os.environ["APP_DATABASE_URL"], connection.settings_dict["NAME"]
        )
        with psycopg.connect(app_url) as runtime, runtime.cursor() as cursor:
            cursor.execute("SELECT session_user, current_user")
            assert cursor.fetchone() == ("clinic_app", "clinic_app")
            for role in UserClinicRole.Role:
                actor = create_role_actor(rbac_graph, role)
                cursor.execute(
                    "SELECT set_config('app.current_tenant', %s, true), "
                    "set_config('app.current_user_id', %s, true)",
                    [str(rbac_graph.organization_a), str(actor.pk)],
                )
                cursor.execute(sql.SQL("INSERT INTO {} VALUES (1)").format(table))
                with pytest.raises(CheckViolation) as caught, runtime.transaction():
                    cursor.execute(sql.SQL("INSERT INTO {} VALUES (2)").format(table))
                assert caught.value.sqlstate == "23514"
                assert "workflows_bundle_selection_guard()" in (
                    caught.value.diag.context or ""
                )
        with connection.cursor() as cursor:
            cursor.execute("SELECT session_user")
            assert cursor.fetchone()[0] in {"clinic_owner", "clinic_super"}
            cursor.execute(sql.SQL("INSERT INTO {} VALUES (2)").format(table))
            assert cursor.rowcount == 1
    finally:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("DROP TABLE {}").format(table))
