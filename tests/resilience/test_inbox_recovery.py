"""Accepted, unbound inbox work remains ineligible for a new provider key."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.comms.inbox_services import InboxRefusedError, Reason, retry_message
from apps.comms.models import IntegrationOperation
from apps.comms.tasks import recover_pending_operations
from apps.identity.models import User
from django.utils import timezone

from comms.support import InboxWorld, manager_of
from comms.test_inbox import _only_conversation, _send, world
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from renewal.broker_support import broker, broker_url, delivery, worker
from resilience.test_broker_worker_death import _accepted_before_sigkill, pytestmark

if TYPE_CHECKING:
    from pathlib import Path

    from redis import Redis

    from renewal.broker_support import Worker

__all__ = ("broker", "broker_url", "pytestmark", "worker", "world")


@pytest.mark.parametrize("offboard", [False, True])
def test_no_fresh_key_after_accepted_inbox_worker_dies(
    world: InboxWorld,
    broker: Redis,
    worker: Worker,
    tmp_path: Path,
    *,
    offboard: bool,
) -> None:
    graph = world.graph
    actor = manager_of(graph)
    world.receive(world.message_body(text="Oi"))
    message = _send(graph, _only_conversation(graph).pk, body="Olá")
    with owner_context(graph.organization_a):
        operation = IntegrationOperation.objects.get(subject_id=message.message_id)
    ledger = tmp_path / "remote-effects"
    with worker.start(CLINIC_GATE_EFFECT_LEDGER=str(ledger)) as process:
        with _accepted_before_sigkill(broker, process.pid) as task_id:
            assert ledger.read_text().splitlines() == [
                f"synthetic:whatsapp:{operation.pk}"
            ]
            if offboard:
                User.objects.filter(pk=actor).update(is_active=False)
        redelivery = delivery(broker, operation.pk)
        assert (redelivery.task_id, redelivery.redelivered) == (task_id, True)
        for _ in range(operation.max_attempts + 2):
            with owner_context(graph.organization_a):
                IntegrationOperation.objects.filter(pk=operation.pk).update(
                    updated_at=timezone.now() - timedelta(minutes=2)
                )
            with runtime_role():
                published = recover_pending_operations()
            if published:
                delivery(broker, operation.pk)
    if offboard:
        User.objects.filter(pk=actor).update(is_active=True)
    with owner_context(graph.organization_a):
        operation.refresh_from_db()
        retained = (
            operation.status,
            operation.last_error,
            operation.provider_reference,
        )
    assert retained == ("in_progress", "outcome_unknown", None)
    with (
        as_actor(graph, actor),
        pytest.raises(InboxRefusedError, match=Reason.NOT_FAILED),
    ):
        retry_message(
            clinic_id=graph.clinic_a, message_id=message.message_id, command_id=uuid4()
        )
    assert ledger.read_text().splitlines() == [f"synthetic:whatsapp:{operation.pk}"]
