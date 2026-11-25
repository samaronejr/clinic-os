"""Native analyst provenance repeats the pinned output permission checks."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

import pytest
from apps.ai.contracts import GatewayRefusedError, Purpose
from apps.ai.models import AIInvocation
from apps.ai.pending_access import command_rows, workflow
from apps.analytics.analyst import display_proposal, request_analyst, review_proposal
from apps.analytics.contracts import AnalyticsAccessDeniedError, AnalyticsConflictError
from apps.analytics.models import AnalystReview, ReadModelRefresh
from apps.core.integration import execute_operation
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, transaction

from ai.pending_observation import count_rows
from ai.retained_rollout_support import (
    INVALIDATIONS,
    Invalidation,
    authorize_native,
    invalidate,
)
from analytics.test_analyst import analyst_world
from analytics.test_metrics import metric_query
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from apps.analytics.analyst import Proposal

    from ai.support import GatewayWorld

__all__ = ("analyst_world",)
pytestmark = pytest.mark.django_db(transaction=True)


def native_review(
    world: GatewayWorld, invocation_id: UUID, ready: Proposal, *, clinic_id: UUID
) -> None:
    """Bypass display/read/review Python; use the real INSERT trigger."""
    authorize_native(
        invocation_id=invocation_id,
        clinic_id=clinic_id,
        purpose=Purpose.ANALYST,
    )
    query = ready.query
    assert query is not None
    with workflow(world.graph.clinic_a):
        command_rows(AnalystReview.objects.all(), "clinic_id").create(
            organization_id=world.graph.organization_a,
            clinic_id=world.graph.clinic_a,
            user_id=world.administrator,
            invocation_id=invocation_id,
            descriptor=query.payload(),
            window_start=query.start,
            window_end=query.end,
            authorization_version=ready.authorization_version,
            decision="accepted",
        )


@pytest.mark.parametrize("mode", INVALIDATIONS)
@pytest.mark.parametrize("native", [False, True], ids=["python", "native"])
def test_retained_analyst_review_cannot_borrow_current_model_permission(
    analyst_world: GatewayWorld,
    monkeypatch: pytest.MonkeyPatch,
    mode: Invalidation,
    native: bool,
) -> None:
    world, graph = analyst_world, analyst_world.graph
    with runtime_role(), tenant_context(world.administrator, graph.organization_a):
        requested = request_analyst(
            clinic_id=graph.clinic_a, question="faltas", query=metric_query("no_show")
        )
        assert requested.invocation_id is not None
        invocation = AIInvocation.objects.get(pk=requested.invocation_id)
        operation = invocation.operation_id
    assert operation is not None
    with runtime_role():
        assert execute_operation(operation_id=operation) == "succeeded"
    with runtime_role(), tenant_context(world.administrator, graph.organization_a):
        ready = display_proposal(clinic_id=graph.clinic_a, invocation_id=invocation.pk)
        assert ready.state == "review"
        assert ready.query is not None
        with transaction.atomic():
            if native:
                native_review(world, invocation.pk, ready, clinic_id=graph.clinic_a)
            else:
                assert (
                    review_proposal(
                        clinic_id=graph.clinic_a,
                        invocation_id=invocation.pk,
                        query_digest=ready.query.digest,
                        authorization_version=ready.authorization_version,
                        decision="accepted",
                    )
                    is not None
                )
            assert count_rows(AnalystReview, graph.clinic_a, invocation=invocation) == 1
            transaction.set_rollback(True)
    clinic_id = invalidate(graph, invocation.pk, mode, monkeypatch)
    with runtime_role(), tenant_context(world.administrator, graph.organization_a):
        refused = (
            (DatabaseError,)
            if native
            else (
                (AnalyticsAccessDeniedError,)
                if mode == "foreign_clinic"
                else (GatewayRefusedError, AnalyticsConflictError)
            )
        )
        attempt = (
            partial(native_review, world, invocation.pk, ready, clinic_id=clinic_id)
            if native
            else partial(
                review_proposal,
                clinic_id=clinic_id,
                invocation_id=invocation.pk,
                query_digest=ready.query.digest,
                authorization_version=ready.authorization_version,
                decision="accepted",
            )
        )
        with pytest.raises(refused), transaction.atomic():
            attempt()
    with owner_context(graph.organization_a):
        assert not AnalystReview.objects.filter(invocation=invocation).exists()
        assert not ReadModelRefresh.objects.filter(clinic_id=graph.clinic_a).exists()
