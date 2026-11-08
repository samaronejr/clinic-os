from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.identity.models import User, UserClinicRole
from apps.providers.lifecycle import revoke_version
from apps.retention.services import export_staff_records, release_version
from apps.subscriptions.collection import (
    CollectionUnavailableError,
    bootstrap_synthetic_collection,
    simulate_event,
)
from apps.subscriptions.models import Seat, Subscription, SubscriptionEvent
from apps.subscriptions.services import (
    SubscriptionConflictError,
    cancel_subscription,
    entitlement_status,
    receive_synthetic_event,
    subscription_snapshot,
)
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction
from django.test import override_settings
from django.utils import timezone

from auth.stepup_test_support import verified_request
from identity.permission_support import owner_context, permission_context
from patient_service_support import runtime_role
from provider_gate_support import APPROVER
from renewal.test_encounters import seed
from renewal.test_retention import finalized
from subscriptions.support import clock, event, publish_quota
from subscriptions.test_contracts import administrator

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_trial_active_failure_grace_restriction_cure_and_event_replay(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    actor = administrator(graph)
    key = uuid4()
    first = event(graph, actor, "paid", key)
    replay = event(graph, actor, "paid", key)
    assert first == replay
    assert first.state == "active"
    with pytest.raises(SubscriptionConflictError):
        event(graph, actor, "payment_failed", key)
    assert event(graph, actor, "payment_failed").state == "past_due"
    grace = event(graph, actor, "advance")
    assert grace.state == "grace"
    with permission_context(graph, actor):
        snapshot = subscription_snapshot(clinic_id=graph.clinic_a)
        assert snapshot.notice_code == "payment_grace_deadline"
        deadline = snapshot.grace_ends_at
    assert deadline is not None
    with pytest.raises(SubscriptionConflictError):
        event(graph, actor, "advance")
    with clock(deadline + timedelta(seconds=1)):
        with permission_context(graph, actor):
            assert not entitlement_status(
                clinic_id=graph.clinic_a, meter="ai_tokens_in"
            ).available
            assert entitlement_status(
                clinic_id=graph.clinic_a, meter="record_export"
            ).available
        assert event(graph, actor, "advance").state == "restricted"
    assert event(graph, actor, "paid").state == "active"
    with owner_context(graph.organization_a):
        assert SubscriptionEvent.objects.filter(command_id=key).count() == 1
        assert (
            AuditEvent.objects.filter(
                organization_id=graph.organization_a,
                event_type="subscriptions.subscription.changed",
            ).count()
            == 5
        )


def test_restricted_subscription_keeps_manual_notes_record_read_and_real_export(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    actor = administrator(graph)
    appointment, template = seed(graph)
    with clock(timezone.now() + timedelta(days=31)):
        assert event(graph, actor, "advance").state == "restricted"
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = finalized(graph, appointment, template, request)
        release_version(clinic_id=graph.clinic_a, version_id=version.pk)
        package = export_staff_records(
            clinic_id=graph.clinic_a,
            patient_id=version.document.encounter.patient_id,
        )
        assert package.export.record_count == 1
        assert package.data
        assert version.state == "finalized"


def test_trial_expiry_cancel_offboard_never_deletes_records_or_memberships(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    actor = administrator(graph)
    with owner_context(graph.organization_a):
        membership_count = UserClinicRole.objects.count()
        seat_count = Seat.objects.filter(organization_id=graph.organization_a).count()
    with permission_context(graph, actor):
        cancelled = cancel_subscription(
            clinic_id=graph.clinic_a, expected_revision=1, command_id=uuid4()
        )
        assert cancelled.state == "cancelled"
    assert event(graph, actor, "offboard").state == "offboarded"
    with permission_context(graph, actor):
        assert entitlement_status(
            clinic_id=graph.clinic_a, meter="clinical_core"
        ).available
        assert entitlement_status(
            clinic_id=graph.clinic_a, meter="reconciliation"
        ).available
        assert not entitlement_status(
            clinic_id=graph.clinic_a, meter="ai_tokens_in"
        ).available
        assert UserClinicRole.objects.count() == membership_count
        assert Seat.objects.count() == seat_count


def test_seats_follow_membership_revocation_and_cannot_be_forged(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    actor = administrator(graph)
    with permission_context(graph, actor):
        occupied = subscription_snapshot(clinic_id=graph.clinic_a).seats
    publish_quota(graph, actor, "seats", occupied)
    user = User.objects.create(username=f"synthetic-seat-{uuid4().hex}")
    with owner_context(graph.organization_a):
        with pytest.raises(DatabaseError), transaction.atomic():
            UserClinicRole.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                user=user,
                role="receptionist",
            )
        membership = UserClinicRole.objects.get(
            user_id=graph.shared_user, clinic_id=graph.clinic_a
        )
        membership.revoked_at = timezone.now()
        membership.save(update_fields=("revoked_at",))
        replacement = UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user=user,
            role="receptionist",
        )
        assert Seat.objects.filter(membership=replacement).exists()
    with permission_context(graph, actor):
        assert subscription_snapshot(clinic_id=graph.clinic_a).seats == occupied
        with pytest.raises(DatabaseError), transaction.atomic():
            Seat.objects.update(membership_id=uuid4())
        with (
            pytest.raises(DatabaseError),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT * FROM clinic_app.subscription_platform_event(%s,%s,%s,'paid')",
                [
                    graph.clinic_a,
                    Subscription.objects.get(organization_id=graph.organization_a).pk,
                    uuid4(),
                ],
            )


def test_cancel_never_cures_restriction_and_active_cancellation_is_scheduled(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    actor = administrator(graph)
    event(graph, actor, "paid")
    with permission_context(graph, actor):
        cancel_subscription(
            clinic_id=graph.clinic_a, expected_revision=2, command_id=uuid4()
        )
        cancelled = subscription_snapshot(clinic_id=graph.clinic_a)
        assert cancelled.cancellation_at is not None
        assert cancelled.cancellation_at > timezone.now()
        assert entitlement_status(
            clinic_id=graph.clinic_a, meter="ai_tokens_in"
        ).available
    with pytest.raises(SubscriptionConflictError):
        event(graph, actor, "offboard")
    with clock(cancelled.cancellation_at + timedelta(seconds=1)):
        assert event(graph, actor, "offboard").state == "offboarded"
    # A separate organization in the same real database proves a restricted
    # subscription cannot restore optional features by requesting cancellation.
    with owner_context(graph.organization_b), connection.cursor() as cursor:
        UserClinicRole.objects.create(
            organization_id=graph.organization_b,
            clinic_id=graph.clinic_c,
            user_id=actor,
            role="org_admin",
        )
        cursor.execute("SELECT set_config('app.current_user_id',%s,true)", [str(actor)])
        subscription = Subscription.objects.get(organization_id=graph.organization_b)
        with clock(timezone.now() + timedelta(days=31)):
            result = receive_synthetic_event(
                clinic_id=graph.clinic_c,
                subscription_id=subscription.pk,
                event_id=uuid4(),
                action="advance",
            )
        assert result.state == "restricted"
    with runtime_role(), tenant_context(actor, graph.organization_b):
        cancel_subscription(
            clinic_id=graph.clinic_c, expected_revision=2, command_id=uuid4()
        )
        assert not entitlement_status(
            clinic_id=graph.clinic_c, meter="ai_tokens_in"
        ).available
        assert entitlement_status(
            clinic_id=graph.clinic_c, meter="record_export"
        ).available


def test_collection_requires_current_synthetic_registry_and_refuses_live(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    with pytest.raises(CollectionUnavailableError):
        simulate_event(
            clinic_id=graph.clinic_a,
            subscription_id=uuid4(),
            event_id=uuid4(),
            action="paid",
        )
    bootstrap_synthetic_collection()
    subscription, key = uuid4(), uuid4()
    first = simulate_event(
        clinic_id=graph.clinic_a,
        subscription_id=subscription,
        event_id=key,
        action="paid",
    )
    assert first == simulate_event(
        clinic_id=graph.clinic_a,
        subscription_id=subscription,
        event_id=key,
        action="paid",
    )
    assert first.synthetic
    assert not first.real_enabled
    assert first.gate == "BLOCKED-ON-EG"
    with (
        override_settings(CLINIC_DATA_MODE="live"),
        pytest.raises(CollectionUnavailableError),
    ):
        simulate_event(
            clinic_id=graph.clinic_a,
            subscription_id=subscription,
            event_id=key,
            action="paid",
        )
    revoke_version(
        "saas_collection", decision=APPROVER, reason="Synthetic rehearsal revoked"
    )
    with pytest.raises(CollectionUnavailableError):
        simulate_event(
            clinic_id=graph.clinic_a,
            subscription_id=subscription,
            event_id=key,
            action="paid",
        )
