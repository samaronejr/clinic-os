"""Workflow saves invalidate reception counts only after their transaction commits."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.realtime import transport
from apps.scheduling.reception_queries import view_reception_day
from apps.tenancy.db import tenant_context
from apps.workflows.services import TaskOwner, assign_task, create_task
from django.db import connection, transaction

from patient_service_support import runtime_role
from workflows.permission_support import permission_actor
from workflows.test_tasks import spec

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_task_changes_refresh_reception_only_after_commit(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, _ = permission_actor(rbac_graph, "receptionist")
    events: list[tuple[str, str, int]] = []

    def published(*, topic: str, kind: str, version: int) -> None:
        assert not connection.in_atomic_block
        events.append((topic, kind, version))

    settings.REALTIME_ENABLED = True
    monkeypatch.setattr(transport, "publish", published)
    with runtime_role(), tenant_context(actor, rbac_graph.organization_a):
        task = create_task(
            clinic_id=rbac_graph.clinic_a,
            spec=spec(rbac_graph),
            idempotency_key=uuid4(),
        )
        assign_task(
            clinic_id=rbac_graph.clinic_a,
            task_id=task.pk,
            owner=TaskOwner(user_id=actor),
            expected_revision=task.revision,
        )
        assert view_reception_day(clinic_id=rbac_graph.clinic_a).pending_tasks == 1
        assert events == []
    expected = (f"clinic:{rbac_graph.clinic_a}:queue", "queue", 1)
    inbox = (f"clinic:{rbac_graph.clinic_a}:inbox", "inbox", 1)
    assert events == [expected, inbox, expected, inbox]

    events.clear()
    with runtime_role(), tenant_context(actor, rbac_graph.organization_a):
        with transaction.atomic():
            create_task(
                clinic_id=rbac_graph.clinic_a,
                spec=spec(rbac_graph),
                idempotency_key=uuid4(),
            )
            transaction.set_rollback(True)
        assert events == []
    assert events == []
