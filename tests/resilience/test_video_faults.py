"""Todo 72: a faulted video provider never blocks the in-person record."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.core.integration import register_send_adapter
from apps.teleconsult.adapters import SyntheticRoomAdapter
from apps.teleconsult.services import (
    TeleconsultConflictError,
    refresh_session,
    request_physician_join,
)
from apps.tenancy.db import tenant_context

from patient_service_support import runtime_role
from renewal.test_teleconsult_sessions import (
    _create,
    _kinds,
    _operation,
    _run_room,
    seed,
    synthetic_provider,
)
from renewal.test_video_recovery import assert_notes, saved_note

if TYPE_CHECKING:
    from uuid import UUID

    from apps.comms.adapters import SendResult
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

__all__ = ("synthetic_provider",)
pytestmark = pytest.mark.django_db(transaction=True)


class CountingRoomAdapter(SyntheticRoomAdapter):
    """The real synthetic adapter, counting every provider call it makes."""

    def __init__(self) -> None:
        self.sends = 0

    def send(self, prepared: object, *, operation_id: UUID) -> SendResult:
        self.sends += 1
        return super().send(prepared, operation_id=operation_id)


@pytest.mark.parametrize("mode", ["down", "slow"])
def test_unreachable_video_retries_then_fails_while_the_note_survives(
    rbac_graph: RbacGraph,
    synthetic_provider: SyntheticRoomAdapter,
    settings: SettingsWrapper,
    mode: str,
) -> None:
    graph = rbac_graph
    _, encounter, _, _, _ = seed(graph)
    note = saved_note(graph, encounter)
    session = _create(graph, encounter)
    settings.CLINIC_SYNTHETIC_FAULTS = {"video": mode}
    assert [_run_room(graph, session) for _ in range(4)] == [
        "retry",
        "retry",
        "retry",
        "failed",
    ]
    settings.CLINIC_SYNTHETIC_FAULTS = {}
    assert _run_room(graph, session) == "skipped"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        failed = refresh_session(session)
        assert (failed.state, failed.failure_reason) == ("failed", "room_unavailable")
        with pytest.raises(TeleconsultConflictError):
            request_physician_join(clinic_id=graph.clinic_a, session_id=session.pk)
    assert _kinds(graph, session) == ["created", "failed"]
    assert_notes(graph, note)


def test_an_ambiguous_room_answer_is_held_and_never_sent_twice(
    rbac_graph: RbacGraph,
    synthetic_provider: SyntheticRoomAdapter,
    settings: SettingsWrapper,
) -> None:
    graph = rbac_graph
    adapter = CountingRoomAdapter()
    register_send_adapter(adapter)
    _, encounter, _, _, _ = seed(graph)
    note = saved_note(graph, encounter)
    session = _create(graph, encounter)
    settings.CLINIC_SYNTHETIC_FAULTS = {"video": "ambiguous"}
    assert _run_room(graph, session) == "reconcile"
    operation = _operation(graph, session)
    assert (operation.status, operation.provider_reference) == ("in_progress", None)
    # Recovery after the provider answers again reconciles; it never re-sends.
    settings.CLINIC_SYNTHETIC_FAULTS = {}
    assert _run_room(graph, session) == "reconcile"
    assert adapter.sends == 1
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        pending = refresh_session(session)
        assert pending.state != "ready"
        assert pending.failure_reason == ""
    assert _kinds(graph, session) == ["created"]
    assert_notes(graph, note)
