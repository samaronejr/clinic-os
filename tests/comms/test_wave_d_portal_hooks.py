"""Merged front-desk and urgent hooks share the real patient transaction."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.comms import portal_messages
from apps.workflows.models import Task
from apps.workflows.urgent_models import UrgentConcern
from django.db import connection

from comms.support import manager_of
from comms.test_portal_messages import (
    _as_patient,
    _portal,
    _portal_audit,
    _rows,
    _thread,
)
from identity.permission_support import owner_context

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("urgent", [False, True])
def test_both_reply_hooks_keep_authority_audit_order_and_replay(
    rbac_graph: RbacGraph,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    urgent: bool,
) -> None:
    graph = rbac_graph
    patient = _portal(graph)
    thread = _thread(graph, graph.clinic_a, patient.enrollment, manager_of(graph))
    calls: list[tuple[str, UUID]] = []
    original_audit = portal_messages._audit

    def audit(
        scope: portal_messages.PortalMessageScope, event: str, record: UUID
    ) -> None:
        original_audit(scope, event, record)
        calls.append((event, record))

    def general_hook(*, clinic_id: UUID, message_id: UUID) -> None:
        assert clinic_id == graph.clinic_a
        scope = portal_messages.portal_message_scope()
        assert scope is not None
        assert scope.session_id == patient.session
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            assert cursor.fetchone() == ("clinic_app",)
        calls.append(("general", message_id))

    def urgent_hook(message_id: UUID) -> None:
        scope = portal_messages.portal_message_scope()
        assert scope is not None
        assert scope.session_id == patient.session
        calls.append(("urgent", message_id))

    monkeypatch.setattr(portal_messages, "_audit", audit)
    monkeypatch.setattr(
        portal_messages, "_REPLY_HOOKS", [*portal_messages._REPLY_HOOKS, general_hook]
    )
    monkeypatch.setattr(
        portal_messages,
        "_URGENT_REPLY_HOOKS",
        [*portal_messages._URGENT_REPLY_HOOKS, urgent_hook],
    )
    key = uuid4()
    body = "Dor no peito e falta de ar agora" if urgent else "Qual o horario?"
    with _as_patient(patient.session):
        receipt = portal_messages.reply_to_portal_conversation(
            clinic_id=graph.clinic_a,
            conversation_id=thread,
            body=body,
            idempotency_key=key,
        )
    expected = [
        (portal_messages.REPLY_EVENT, receipt.message_id),
        ("general", receipt.message_id),
    ]
    if urgent:
        expected.extend(
            [
                (portal_messages.ESCALATED_EVENT, thread),
                ("urgent", receipt.message_id),
            ]
        )
    assert calls == expected
    before = _rows(graph), _portal_audit(graph)
    with _as_patient(patient.session):
        replay = portal_messages.reply_to_portal_conversation(
            clinic_id=graph.clinic_a,
            conversation_id=thread,
            body=body,
            idempotency_key=key,
        )
    assert replay.replayed
    assert replay.message_id == receipt.message_id
    assert calls == expected
    assert (_rows(graph), _portal_audit(graph)) == before


@pytest.mark.parametrize("failing_hook", ["general", "urgent"])
def test_either_hook_failure_rolls_back_the_shared_reply(
    rbac_graph: RbacGraph,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_hook: str,
) -> None:
    graph = rbac_graph
    patient = _portal(graph)
    thread = _thread(graph, graph.clinic_a, patient.enrollment, manager_of(graph))
    should_fail = True
    failure = RuntimeError(failing_hook)

    def general_hook(*, clinic_id: UUID, message_id: UUID) -> None:
        if should_fail and failing_hook == "general":
            raise failure

    def urgent_hook(message_id: UUID) -> None:
        if should_fail and failing_hook == "urgent":
            raise failure

    def domain_rows() -> tuple[list[UUID], list[UUID]]:
        with owner_context(graph.organization_a):
            return (
                list(Task.objects.order_by("pk").values_list("pk", flat=True)),
                list(UrgentConcern.objects.order_by("pk").values_list("pk", flat=True)),
            )

    monkeypatch.setattr(
        portal_messages, "_REPLY_HOOKS", [*portal_messages._REPLY_HOOKS, general_hook]
    )
    monkeypatch.setattr(
        portal_messages,
        "_URGENT_REPLY_HOOKS",
        [*portal_messages._URGENT_REPLY_HOOKS, urgent_hook],
    )
    before = _rows(graph), _portal_audit(graph), domain_rows()
    key = uuid4()
    with pytest.raises(RuntimeError, match=failing_hook), _as_patient(patient.session):
        portal_messages.reply_to_portal_conversation(
            clinic_id=graph.clinic_a,
            conversation_id=thread,
            body="Dor no peito e falta de ar agora",
            idempotency_key=key,
        )
    assert (_rows(graph), _portal_audit(graph), domain_rows()) == before
    should_fail = False
    with _as_patient(patient.session):
        receipt = portal_messages.reply_to_portal_conversation(
            clinic_id=graph.clinic_a,
            conversation_id=thread,
            body="Dor no peito e falta de ar agora",
            idempotency_key=key,
        )
    assert not receipt.replayed
