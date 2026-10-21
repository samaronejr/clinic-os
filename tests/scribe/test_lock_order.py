"""Deterministic two-connection proof of discard and worker lock ordering."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.services import record_phase1_event
from apps.core import patient_context
from apps.core.patient_context import ContextSwitch, SwitchConsequence
from apps.scribe.models import AudioChunk
from apps.scribe.services import control_recording
from apps.scribe.storage import media_storage
from apps.tenancy.db import tenant_context
from django.db import connection, connections

from patient_service_support import runtime_role
from scribe.test_ingest import chunk, media, receive, world

if TYPE_CHECKING:
    from collections.abc import Callable

    from rbac_fixtures import RbacGraph

__all__ = ("media",)
pytestmark = pytest.mark.django_db(transaction=True)


def test_all_switch_decisions_precede_the_first_discard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, int]] = []

    class Guard:
        def __init__(self, number: int) -> None:
            self.number = number

        def check(self, _switch: ContextSwitch) -> SwitchConsequence:
            events.append(("check", self.number))
            return SwitchConsequence(str(self.number), "")

        def discard(self, _switch: ContextSwitch) -> None:
            events.append(("discard", self.number))

    monkeypatch.setattr(
        patient_context, "_GUARDS", {"first": Guard(1), "second": Guard(2)}
    )
    patient_context.discard_bound_work(ContextSwitch(uuid4(), uuid4(), uuid4()))
    assert events == [
        ("check", 1),
        ("check", 2),
        ("discard", 1),
        ("discard", 2),
    ]


def test_discard_waits_for_worker_chunk_before_taking_the_audit_lock(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    scope = world(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        receive(graph, scope, chunk(scope))
        item = AudioChunk.objects.get(session_id=scope.session)
        # Match the real adapter's already-completed external ciphertext write.
        media_storage(clinic_id=graph.clinic_a).put(
            item.storage_key, bytes(item.encrypted_data)
        )
    locked = Event()
    attempted = Event()

    def worker() -> None:
        try:
            with runtime_role(), tenant_context(graph.physician, graph.organization_a):
                stored = AudioChunk.objects.select_for_update().get(pk=item.pk)
                stored.state = "accepted"
                stored.encrypted_data = b""
                stored.save(update_fields=("state", "encrypted_data"))
                locked.set()
                assert attempted.wait(timeout=15)
                record_phase1_event(
                    "scribe.chunk.accepted",
                    clinic_id=graph.clinic_a,
                    affected_record_id=stored.pk,
                )
        finally:
            connections.close_all()

    def observe(
        execute: Callable[..., object],
        query: str,
        params: object,
        many: bool,
        context: dict[str, object],
    ) -> object:
        lowered = query.lower()
        if "scribe_audiochunk" in lowered and (
            "for update" in lowered or lowered.startswith("update")
        ):
            attempted.set()
        return execute(query, params, many, context)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        assert locked.wait(timeout=15)
        with (
            runtime_role(),
            tenant_context(graph.physician, graph.organization_a),
            connection.execute_wrapper(observe),
        ):
            result = control_recording(
                clinic_id=graph.clinic_a,
                session_id=scope.session,
                encounter_token=scope.token,
                command_id=uuid4(),
                action="cancel",
                expected_epoch=1,
            )
            assert result.state == "stopped"
        future.result(timeout=15)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        item.refresh_from_db()
        assert (item.state, bytes(item.wrapped_key)) == ("rejected", b"")
