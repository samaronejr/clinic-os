"""Independent FORCE RLS, approved-view and raw-runtime permission proofs."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import psycopg
import pytest
from apps.analytics.models import MetricDefinitionVersion, SavedReport
from apps.analytics.services import refresh_metric
from apps.identity.models import User, UserClinicRole
from django.db import DatabaseError, connection, transaction
from django.utils import timezone
from psycopg import sql
from psycopg.errors import InsufficientPrivilege
from psycopg.types.json import Jsonb

from analytics.test_metrics import metric_query
from billing.journal_support import finance_scope, finance_setup, invoice_terms
from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)

if TYPE_CHECKING:
    from billing.journal_support import FinanceSetup

__all__ = ("finance_setup",)
pytestmark = pytest.mark.django_db(transaction=True)
TABLES = {
    "analytics_metricdefinitionversion",
    "analytics_readmodelrefresh",
    "analytics_savedreport",
    "analytics_analystreview",
}


def test_authorization_fingerprint_uses_the_same_exact_sha256_bytes() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.digest(%s,'sha256')=sha256(convert_to(%s,'UTF8'))",
            [
                "analytics-auth-v1:clinic_manager:2",
                "analytics-auth-v1:clinic_manager:2",
            ],
        )
        assert cursor.fetchone() == (True,)


def test_every_reporting_table_and_view_has_exact_posture() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,"
            "c.relowner::regrole::text "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='clinic_app' AND c.relkind='r' "
            "AND c.relname LIKE 'analytics_%%'"
        )
        assert set(cursor.fetchall()) == {
            (table, True, True, "clinic_owner") for table in TABLES
        }
        cursor.execute(
            "SELECT tablename,policyname,roles FROM pg_policies "
            "WHERE schemaname='clinic_app' AND tablename LIKE 'analytics_%%'"
        )
        assert {(row[0], row[1], tuple(row[2])) for row in cursor.fetchall()} == {
            (table, "analytics_scope", ("clinic_app",)) for table in TABLES
        }
        cursor.execute(
            "SELECT c.relname,c.relowner::regrole::text,c.reloptions, "
            "has_table_privilege('clinic_app',c.oid,'SELECT') "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='clinic_app' AND c.relkind='v' "
            "AND c.relname LIKE 'analytics_%%'"
        )
        assert {
            (row[0], row[1], tuple(row[2]), row[3]) for row in cursor.fetchall()
        } == {
            (
                "analytics_metricfact",
                "clinic_resolver",
                ("security_barrier=true",),
                True,
            ),
            (
                "analytics_usagefact",
                "clinic_resolver",
                ("security_barrier=true",),
                False,
            ),
        }


@pytest.mark.parametrize("role", UserClinicRole.Role.values)
@pytest.mark.parametrize("state", ["active", "inactive", "revoked"])
def test_sql_reporting_boundary_matrix(
    finance_setup: FinanceSetup, role: str, state: str
) -> None:
    rbac_graph = finance_setup.graph
    query = metric_query("no_show")
    invoice_terms(finance_setup)
    actor, _enrollment = permission_actor(rbac_graph, role)
    if state == "inactive":
        User.objects.filter(pk=actor).update(is_active=False)
    if state == "revoked":
        with owner_context(rbac_graph.organization_a):
            UserClinicRole.objects.filter(user_id=actor).update(
                revoked_at=timezone.now()
            )
    allowed = state == "active" and role in {
        "owner",
        "org_admin",
        "clinic_manager",
        "clinic_admin",
        "finance",
    }
    with permission_context(rbac_graph, actor), connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.analytics_allowed(%s,'no_show')", [rbac_graph.clinic_a]
        )
        assert cursor.fetchone() == (allowed,)
        cursor.execute(
            "SELECT count(*) FROM clinic_app.analytics_metricfact "
            "WHERE clinic_id=%s AND metric_id='no_show'",
            [rbac_graph.clinic_a],
        )
        assert cursor.fetchone() == (int(allowed),)
        if allowed:
            cursor.execute(
                "SELECT * FROM clinic_app.analytics_refresh(%s,%s::jsonb,%s,%s,%s)",
                [rbac_graph.clinic_a, query.encode(), uuid4(), query.start, query.end],
            )
            assert cursor.fetchone() is not None
        else:
            with pytest.raises(DatabaseError), transaction.atomic():
                cursor.execute(
                    "SELECT clinic_app.analytics_authorization(%s)",
                    [rbac_graph.clinic_a],
                )
            with pytest.raises(DatabaseError), transaction.atomic():
                cursor.execute(
                    "SELECT * FROM clinic_app.analytics_refresh(%s,%s::jsonb,%s,%s,%s)",
                    [
                        rbac_graph.clinic_a,
                        query.encode(),
                        uuid4(),
                        query.start,
                        query.end,
                    ],
                )
        for clinic in (rbac_graph.clinic_c, uuid4()):
            cursor.execute(
                "SELECT clinic_app.analytics_allowed(%s,'no_show')", [clinic]
            )
            assert cursor.fetchone() == (False,)


def test_saved_report_trigger_rejects_forged_scope_and_owner_history_is_immutable(
    finance_setup: FinanceSetup,
) -> None:
    setup = finance_setup
    query = metric_query("no_show")
    with finance_scope(setup):
        refresh_metric(clinic_id=setup.clinic_id, query=query, command_id=uuid4())
        definition = MetricDefinitionVersion.objects.get(clinic_id=setup.clinic_id)
        with pytest.raises(DatabaseError), transaction.atomic():
            SavedReport.objects.create(
                organization_id=setup.graph.organization_a,
                clinic_id=setup.clinic_id,
                user_id=setup.graph.physician,
                definition=definition,
                descriptor=query.payload(),
                window_start=query.start,
                window_end=query.end,
            )
        refresh_metric(
            clinic_id=setup.clinic_id,
            query=metric_query("receivables"),
            command_id=uuid4(),
        )
        wrong_definition = MetricDefinitionVersion.objects.get(
            clinic_id=setup.clinic_id,
            metric_id="receivables",
        )
        with pytest.raises(DatabaseError), transaction.atomic():
            SavedReport.objects.create(
                organization_id=setup.graph.organization_a,
                clinic_id=setup.clinic_id,
                user_id=setup.graph.clinic_admin,
                definition=wrong_definition,
                descriptor=query.payload(),
                window_start=query.start,
                window_end=query.end,
            )
    with owner_context(setup.graph.organization_a), connection.cursor() as cursor:
        for table in sorted(TABLES):
            with pytest.raises(DatabaseError), transaction.atomic():
                cursor.execute(
                    sql.SQL("DELETE FROM clinic_app.{} WHERE false")
                    .format(sql.Identifier(table))
                    .as_string(connection.connection)
                )


def test_saved_report_trigger_itself_rejects_an_inactive_actor(
    finance_setup: FinanceSetup,
    superuser_database_url: str,
) -> None:
    setup = finance_setup
    query = metric_query("no_show")
    with finance_scope(setup):
        refresh_metric(clinic_id=setup.clinic_id, query=query, command_id=uuid4())
        definition = MetricDefinitionVersion.objects.get(clinic_id=setup.clinic_id)
    with psycopg.connect(superuser_database_url) as raw:
        # Bypass RLS on the explicit test connection to reach the trigger itself.
        # No Python current-actor or tenant entry gate can mask that decision.
        assert raw.execute(
            "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user"
        ).fetchone() == (True,)
        raw.execute(
            "SELECT set_config('app.current_tenant',%s,true),"
            "set_config('app.current_user_id',%s,true)",
            [str(setup.graph.organization_a), str(setup.graph.clinic_admin)],
        )
        statement = (
            "INSERT INTO clinic_app.analytics_savedreport "
            "(id,organization_id,clinic_id,user_id,definition_id,"
            "descriptor,window_start,window_end,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp())"
        )
        values = (
            setup.graph.organization_a,
            setup.clinic_id,
            setup.graph.clinic_admin,
            definition.pk,
            Jsonb(query.payload()),
            query.start,
            query.end,
        )
        raw.execute(statement, (uuid4(), *values))
        raw.execute(
            "UPDATE clinic_app.identity_user SET is_active=false WHERE id=%s",
            [setup.graph.clinic_admin],
        )
        with (
            pytest.raises(InsufficientPrivilege, match="metric access denied"),
            raw.transaction(),
        ):
            raw.execute(statement, (uuid4(), *values))


@pytest.mark.parametrize("text", ["now", "today"])
def test_window_clock_text_is_refused_even_beside_typed_bounds(
    finance_setup: FinanceSetup, text: str
) -> None:
    # Given: a descriptor whose window start is clock text, beside typed bounds.
    setup = finance_setup
    query = metric_query("no_show")
    payload = query.payload()
    window = cast("dict[str, str]", payload["window"])
    forged = payload | {"window": window | {"start": text}}
    with finance_scope(setup):
        refresh_metric(clinic_id=setup.clinic_id, query=query, command_id=uuid4())
        definition = MetricDefinitionVersion.objects.get(clinic_id=setup.clinic_id)
        # When/Then: the refresh never parses window text and refuses it.
        with (
            pytest.raises(DatabaseError, match="invalid metric query"),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT * FROM clinic_app.analytics_refresh(%s,%s::jsonb,%s,%s,%s)",
                [setup.clinic_id, json.dumps(forged), uuid4(), query.start, query.end],
            )
        # And: the history guard refuses the same descriptor on a saved report.
        with (
            pytest.raises(DatabaseError, match="invalid metric query"),
            transaction.atomic(),
        ):
            SavedReport.objects.create(
                organization_id=setup.graph.organization_a,
                clinic_id=setup.clinic_id,
                user_id=setup.graph.clinic_admin,
                definition=definition,
                descriptor=forged,
                window_start=query.start,
                window_end=query.end,
            )
