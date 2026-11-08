from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai.models import AIInvocation
from apps.ai.tasks import run_operation
from apps.core.integration import execute_operation
from apps.ehr.models import ClinicalAttachment
from apps.subscriptions.models import UsageEvent, UsageRollup
from apps.subscriptions.services import entitlement_status
from apps.teleconsult.models import TeleconsultSession
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction

from ai.support import SENTINEL, enqueue, seed
from comms import test_inbox as inbox_tests
from comms.support import inbox_account
from identity.permission_support import owner_context, permission_context
from patient_service_support import runtime_role
from renewal.test_attachments import (
    attachment_root,
)
from renewal.test_attachments import (
    test_upload_quarantine_scan_and_exact_download as attachment_flow,
)
from renewal.test_teleconsult_sessions import (
    synthetic_provider,
)
from renewal.test_teleconsult_sessions import (
    test_scoped_lifecycle_room_and_event_history as video_flow,
)
from subscriptions.support import publish_quota

if TYPE_CHECKING:
    from pathlib import Path

    from apps.teleconsult.adapters import SyntheticRoomAdapter
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

__all__ = ("attachment_root", "synthetic_provider")
pytestmark = pytest.mark.django_db(transaction=True)


def test_ai_completion_and_replay_meter_exactly_once_without_content(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = rbac_graph
    world = seed(graph, monkeypatch)
    result = enqueue(world)
    assert result.invocation_id is not None
    assert result.operation_id is not None
    monkeypatch.setattr("apps.ai.tasks.fair_acquire", lambda **_kwargs: True)
    with runtime_role():
        assert run_operation(operation_id=str(result.operation_id)) == "succeeded"
        assert execute_operation(result.operation_id) == "skipped"
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        invocation = AIInvocation.objects.get(pk=result.invocation_id)
        facts = dict(
            UsageEvent.objects.filter(source_ref=invocation.pk).values_list(
                "meter", "quantity"
            )
        )
        assert facts == {
            "ai_tokens_in": invocation.tokens_in,
            "ai_tokens_out": invocation.tokens_out,
            "ai_cost_micros": invocation.cost_micros,
        }
        assert dict(UsageRollup.objects.values_list("meter", "quantity")) == facts
        assert SENTINEL not in json.dumps(
            list(UsageEvent.objects.values()), default=str
        )


def test_outstanding_reservations_consume_quota_before_dispatch(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = rbac_graph
    world = seed(graph, monkeypatch)
    publish_quota(graph, world.administrator, "ai_tokens_in", 100)
    first = enqueue(world)
    assert first.invocation_id is not None
    refused = enqueue(world)
    assert refused.invocation_id is None
    assert refused.reason == "budget_exhausted"
    with permission_context(graph, graph.shared_user):
        assert entitlement_status(
            clinic_id=graph.clinic_a, meter="clinical_core"
        ).available
        status = entitlement_status(clinic_id=graph.clinic_a, meter="ai_tokens_in")
        assert status.used == status.quota == 100
        assert status.alert
        assert AIInvocation.objects.count() == 1


def test_late_usage_rolls_into_occurred_month_and_changed_replay_conflicts(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    source = uuid4()
    occurred = datetime(2025, 12, 31, 23, 59, tzinfo=UTC)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_resolver")
        for _ in range(2):
            cursor.execute(
                "SELECT clinic_app.subscription_record_usage(%s,'messages',3,%s,%s)",
                [graph.organization_a, occurred, source],
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            cursor.execute(
                "SELECT clinic_app.subscription_record_usage(%s,'messages',4,%s,%s)",
                [graph.organization_a, occurred, source],
            )
    with owner_context(graph.organization_a):
        assert UsageEvent.objects.get(source_ref=source).quantity == 3
        rollup = UsageRollup.objects.get(organization_id=graph.organization_a)
        assert str(rollup.period) == "2025-12-01"
        assert rollup.quantity == 3
    with permission_context(graph, graph.shared_user), connection.cursor() as cursor:
        with pytest.raises(DatabaseError), transaction.atomic():
            cursor.execute(
                "SELECT clinic_app.subscription_record_usage(%s,'messages',3,%s,%s)",
                [graph.organization_a, occurred, uuid4()],
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            UsageEvent.objects.create(
                organization_id=graph.organization_a,
                meter="messages",
                quantity=1,
                occurred_at=occurred,
                source_ref=uuid4(),
            )


def test_video_terminal_replay_records_two_participant_elapsed_seconds(
    rbac_graph: RbacGraph,
    synthetic_provider: SyntheticRoomAdapter,
) -> None:
    video_flow(rbac_graph, synthetic_provider)
    with owner_context(rbac_graph.organization_a):
        session = TeleconsultSession.objects.get()
        assert session.ended_at is not None
        assert session.started_at is not None
        quantity = (
            math.ceil(max(0, (session.ended_at - session.started_at).total_seconds()))
            * 2
        )
        assert (
            UsageEvent.objects.get(
                source_ref=session.pk, meter="video_seconds"
            ).quantity
            == quantity
        )
        assert UsageEvent.objects.count() == 1


def test_message_delivery_status_replay_does_not_charge_another_send(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
) -> None:
    world = inbox_account(rbac_graph, settings)
    inbox_tests.test_status_webhook_advances_delivery_and_records_the_providers_meter(
        world
    )
    with owner_context(rbac_graph.organization_a):
        assert list(UsageEvent.objects.values_list("meter", "quantity")) == [
            ("messages", 1)
        ]


def test_attachment_scan_and_download_never_charge_bytes_twice(
    rbac_graph: RbacGraph,
    attachment_root: Path,
) -> None:
    attachment_flow(rbac_graph, attachment_root)
    with owner_context(rbac_graph.organization_a):
        attachment = ClinicalAttachment.objects.get()
        fact = UsageEvent.objects.get(source_ref=attachment.pk)
        assert (fact.meter, fact.quantity) == ("storage_bytes", attachment.size_bytes)
        assert UsageEvent.objects.count() == 1
