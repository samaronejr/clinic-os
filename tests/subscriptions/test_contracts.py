from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import User, UserClinicRole
from apps.subscriptions.models import Entitlement, Plan, Subscription
from apps.subscriptions.services import (
    SubscriptionConflictError,
    SubscriptionDeniedError,
    change_plan,
    entitlement_status,
    subscription_snapshot,
)
from django.db import DatabaseError, connection, transaction

from identity.permission_support import owner_context, permission_context

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def administrator(graph: RbacGraph) -> UUID:
    actor = User.objects.create(username=f"synthetic-subscriptions-{uuid4().hex}")
    with owner_context(graph.organization_a):
        for clinic in (graph.clinic_a, graph.clinic_b):
            UserClinicRole.objects.create(
                organization_id=graph.organization_a,
                clinic_id=clinic,
                user=actor,
                role="org_admin",
            )
    return actor.pk


def test_provisional_trial_is_bounded_and_keeps_clinical_core(
    rbac_graph: RbacGraph,
) -> None:
    with permission_context(rbac_graph, rbac_graph.shared_user):
        snapshot = subscription_snapshot(clinic_id=rbac_graph.clinic_a)
        assert snapshot.state == "trial"
        assert snapshot.provisional
        assert snapshot.trial_ends_at is not None
        assert entitlement_status(
            clinic_id=rbac_graph.clinic_a, meter="clinical_core", quantity=1
        ).available
        assert not entitlement_status(
            clinic_id=rbac_graph.clinic_a, meter="ai_tokens_in", quantity=10**12
        ).available


def test_org_admin_selects_packaging_but_cannot_write_entitlements(
    rbac_graph: RbacGraph,
) -> None:
    actor = administrator(rbac_graph)
    command = uuid4()
    with permission_context(rbac_graph, actor):
        first = change_plan(
            clinic_id=rbac_graph.clinic_a,
            plan_code="core_operations",
            expected_revision=1,
            command_id=command,
        )
        replay = change_plan(
            clinic_id=rbac_graph.clinic_a,
            plan_code="core_operations",
            expected_revision=1,
            command_id=command,
        )
        assert first == replay
        with pytest.raises(SubscriptionConflictError):
            change_plan(
                clinic_id=rbac_graph.clinic_a,
                plan_code="core_operations",
                expected_revision=2,
                command_id=command,
            )
        assert not entitlement_status(
            clinic_id=rbac_graph.clinic_a, meter="ai_tokens_in", quantity=1
        ).available
        with pytest.raises(DatabaseError), transaction.atomic():
            Entitlement.objects.update(quota=10**12)
        assert set(Plan.objects.values_list("code", flat=True)) == {
            "core_operations",
            "intelligence_usage",
            "network_governance",
        }
        assert Subscription.objects.get(pk=first.subscription_id).revision == 2


def test_plan_management_requires_organization_wide_current_authority(
    rbac_graph: RbacGraph,
) -> None:
    with permission_context(rbac_graph, rbac_graph.shared_user):
        for clinic in (rbac_graph.clinic_a, rbac_graph.clinic_c, uuid4()):
            with pytest.raises(SubscriptionDeniedError):
                change_plan(
                    clinic_id=clinic,
                    plan_code="network_governance",
                    expected_revision=1,
                    command_id=uuid4(),
                )


def test_subscription_sql_readers_refuse_an_inactive_actor(
    rbac_graph: RbacGraph,
) -> None:
    actor = administrator(rbac_graph)
    decisions = []
    for active in (True, False, True):
        User.objects.filter(pk=actor).update(is_active=active)
        with permission_context(rbac_graph, actor), connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.subscription_scope(%s,false) IS NOT NULL, "
                "clinic_app.subscription_scope(%s,true) IS NOT NULL, "
                "clinic_app.subscription_entitled(%s,'ai_tokens_in',1)",
                [rbac_graph.clinic_a] * 3,
            )
            decisions.append(cursor.fetchone())
    assert decisions == [(True, True, True), (False, False, False), (True, True, True)]
