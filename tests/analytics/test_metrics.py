"""Real-role authorization, exact journal sources and immutable read models."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.analytics.catalog import METRICS
from apps.analytics.contracts import (
    AnalyticsAccessDeniedError,
    AnalyticsConflictError,
    Query,
)
from apps.analytics.models import ReadModelRefresh
from apps.analytics.services import (
    authorize,
    definitions,
    drill_down,
    query_metric,
    refresh_metric,
)
from apps.billing.collections import observe_settlement
from apps.billing.money_types import SettlementObservation
from apps.identity.models import RoleGrant, UserClinicRole
from apps.scheduling.services import mark_no_show
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from billing.journal_support import (
    allocated,
    finance_scope,
    finance_setup,
    invoice_terms,
)
from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)
from patient_service_support import runtime_role
from scheduling.appointment_service_support import create_synthetic_appointment
from scheduling.clock_support import frozen_sql_clocks

if TYPE_CHECKING:
    from billing.journal_support import FinanceSetup
    from rbac_fixtures import RbacGraph

__all__ = ("finance_setup",)
pytestmark = pytest.mark.django_db(transaction=True)


def metric_query(key: str, *, future: bool = False) -> Query:
    now = datetime(2035, 6, 2, tzinfo=UTC) if future else timezone.now()
    return Query(
        key, now - timedelta(days=1), now + timedelta(days=1), "America/Sao_Paulo"
    )


def test_zero_denominator_is_null_and_refresh_replay_is_immutable(
    finance_setup: FinanceSetup,
) -> None:
    setup = finance_setup
    command = uuid4()
    with finance_scope(setup):
        query = metric_query("no_show")
        first = refresh_metric(
            clinic_id=setup.clinic_id, query=query, command_id=command
        )
        assert first.status == "null"
        assert first.rows[0]["value"] is None
        second = refresh_metric(
            clinic_id=setup.clinic_id, query=query, command_id=command
        )
        assert second.refresh_id == first.refresh_id
        with pytest.raises(AnalyticsConflictError):
            refresh_metric(
                clinic_id=setup.clinic_id,
                query=metric_query("receivables"),
                command_id=command,
            )
        assert query_metric(clinic_id=setup.clinic_id, query=query) == first
        with pytest.raises(DatabaseError), transaction.atomic():
            ReadModelRefresh.objects.filter(pk=first.refresh_id).update(
                quality="complete"
            )


@pytest.mark.parametrize("role", UserClinicRole.Role.values)
def test_metric_permission_is_decided_before_any_source_or_cache(
    rbac_graph: RbacGraph,
    role: str,
) -> None:
    actor, _enrollment = permission_actor(rbac_graph, role)
    allowed = role in {
        "owner",
        "org_admin",
        "clinic_admin",
        "clinic_manager",
        "finance",
    }
    with permission_context(rbac_graph, actor):
        if allowed:
            assert len(definitions(clinic_id=rbac_graph.clinic_a)) == 15
        else:
            with pytest.raises(AnalyticsAccessDeniedError):
                refresh_metric(
                    clinic_id=rbac_graph.clinic_a,
                    query=metric_query("no_show"),
                    command_id=uuid4(),
                )
            assert not ReadModelRefresh.objects.exists()
        with pytest.raises(AnalyticsAccessDeniedError):
            definitions(clinic_id=rbac_graph.clinic_c)
        with pytest.raises(AnalyticsAccessDeniedError):
            definitions(clinic_id=uuid4())


def test_cache_key_changes_with_effective_authorization_version(
    finance_setup: FinanceSetup,
) -> None:
    setup = finance_setup
    query = metric_query("no_show")
    with finance_scope(setup):
        first = refresh_metric(
            clinic_id=setup.clinic_id, query=query, command_id=uuid4()
        )
    with owner_context(setup.graph.organization_a):
        RoleGrant.objects.create(
            organization_id=setup.graph.organization_a,
            clinic_id=setup.clinic_id,
            role="finance",
            permission="charge.create",
            bundle_version=1,
            valid_from=timezone.now() - timedelta(days=1),
        )
    with finance_scope(setup):
        assert authorize(clinic_id=setup.clinic_id)[1] != first.authorization_version
        assert query_metric(clinic_id=setup.clinic_id, query=query) is None
        assert not ReadModelRefresh.objects.filter(pk=first.refresh_id).exists()


def test_finance_is_only_posted_exact_journal_money(
    finance_setup: FinanceSetup,
) -> None:
    setup = finance_setup
    terms = invoice_terms(setup)
    with finance_scope(setup):
        observe_settlement(
            clinic_id=setup.clinic_id,
            observation=SettlementObservation(90000, "patient", "bank", uuid4()),
        )
        before = refresh_metric(
            clinic_id=setup.clinic_id,
            query=metric_query("verified_collections"),
            command_id=uuid4(),
        )
        assert before.rows[0]["value"] == "0"
        allocated(setup, terms, 6000)
        expected = {
            "receivables": "4000",
            "verified_collections": "6000",
            "net_collections": "6000",
            "payout_liabilities": "1800",
        }
        for key, value in expected.items():
            result = refresh_metric(
                clinic_id=setup.clinic_id, query=metric_query(key), command_id=uuid4()
            )
            assert result.rows[0]["value"] == value
        denied = refresh_metric(
            clinic_id=setup.clinic_id, query=metric_query("denials"), command_id=uuid4()
        )
        assert denied.status == "partial"
        assert denied.rows[0]["value"] is None


def test_stale_data_keeps_its_observation_time(
    finance_setup: FinanceSetup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = finance_setup
    query = metric_query("no_show")
    with finance_scope(setup):
        original = refresh_metric(
            clinic_id=setup.clinic_id, query=query, command_id=uuid4()
        )
        monkeypatch.setattr(
            "apps.analytics.services.timezone.now",
            lambda: original.as_of + timedelta(hours=1),
        )
        cached = query_metric(clinic_id=setup.clinic_id, query=query)
        assert cached is not None
        assert cached.status == "stale"
        assert cached.as_of == original.as_of


def test_raw_runtime_cannot_forge_aggregates_or_cross_clinic_sources(
    finance_setup: FinanceSetup,
) -> None:
    setup = finance_setup
    with finance_scope(setup), connection.cursor() as cursor:
        with pytest.raises(DatabaseError), transaction.atomic():
            cursor.execute(
                "INSERT INTO clinic_app.analytics_readmodelrefresh DEFAULT VALUES"
            )
        cursor.execute(
            "SELECT count(*) FROM clinic_app.analytics_metricfact WHERE clinic_id=%s",
            [setup.graph.clinic_c],
        )
        assert cursor.fetchone() == (0,)
    with (
        runtime_role(),
        tenant_context(setup.graph.physician, setup.graph.organization_a),
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT count(*) FROM clinic_app.analytics_metricfact")
        assert cursor.fetchone() == (0,)


def test_resolved_no_show_and_utilization_have_real_denominators(
    finance_setup: FinanceSetup,
    superuser_database_url: str,
) -> None:
    setup = finance_setup
    invoice_terms(setup)
    with (
        runtime_role(),
        tenant_context(setup.graph.shared_user, setup.graph.organization_a),
    ):
        appointment = create_synthetic_appointment(
            setup.appointment,
            start_local="2035-06-02T10:00",
            end_local="2035-06-02T11:00",
        )
    with (
        frozen_sql_clocks(
            superuser_database_url,
            datetime(2035, 6, 2, 16, tzinfo=UTC),
        ),
        runtime_role(),
        tenant_context(setup.graph.shared_user, setup.graph.organization_a),
    ):
        mark_no_show(
            clinic_id=setup.clinic_id,
            appointment_id=appointment.pk,
            expected_revision=appointment.revision,
            command_id=uuid4(),
        )
    with finance_scope(setup):
        query = metric_query("no_show", future=True)
        result = refresh_metric(
            clinic_id=setup.clinic_id, query=query, command_id=uuid4()
        )
        assert result.rows[0]["numerator"] == "1"
        assert result.rows[0]["denominator"] == "2"
        assert result.rows[0]["value"] == "50.00"
        assert {
            row["state"] for row in drill_down(clinic_id=setup.clinic_id, query=query)
        } == {"completed", "no_show"}
        utilization = refresh_metric(
            clinic_id=setup.clinic_id,
            query=metric_query("utilization", future=True),
            command_id=uuid4(),
        )
        assert utilization.rows[0]["value"] == "50.00"


def test_every_initial_metric_executes_an_approved_source_without_guessing(
    finance_setup: FinanceSetup,
) -> None:
    with finance_scope(finance_setup):
        for key in METRICS:
            result = refresh_metric(
                clinic_id=finance_setup.clinic_id,
                query=metric_query(key),
                command_id=uuid4(),
            )
            assert result.status in {"fresh", "null", "partial"}
            if key in {"denials", "support_load", "retained_usage"}:
                assert result.rows[0]["value"] is None
