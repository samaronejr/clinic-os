"""Observe real broker publication and log records around encrypted ingest."""

from __future__ import annotations

import json
from base64 import b64encode
from typing import TYPE_CHECKING

import pytest
from apps.scribe.models import AudioChunk
from apps.scribe.services import ingest_chunk
from apps.tenancy.db import tenant_context
from celery.signals import after_task_publish

from patient_service_support import runtime_role
from scribe.test_ingest import BODY, chunk, media, world

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

__all__ = ("media",)
pytestmark = pytest.mark.django_db(transaction=True)


def test_ingest_publishes_only_the_outbox_reference_and_no_sensitive_log_values(
    rbac_graph: RbacGraph,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given: a real recording and a subscriber before the after-commit publish.
    graph = rbac_graph
    scope = world(graph)
    messages: list[tuple[object, object, str]] = []

    def published(
        *, body: object, headers: object, routing_key: str, **_kwargs: object
    ) -> None:
        messages.append((body, headers, routing_key))

    after_task_publish.connect(
        published, sender="apps.scribe.tasks.execute", weak=False
    )
    try:
        # When: committed ingest sends through the actual synthetic broker.
        with runtime_role(), tenant_context(graph.physician, graph.organization_a):
            result = ingest_chunk(
                clinic_id=graph.clinic_a,
                encounter_token=scope.token,
                chunk=chunk(scope),
            )
            operation = AudioChunk.objects.get(session_id=scope.session).operation_id
    finally:
        after_task_publish.disconnect(published, sender="apps.scribe.tasks.execute")

    # Then: serialized task terms are one stored reference, never audio or token.
    assert result.ack == "received"
    assert len(messages) == 1
    body, _headers, queue = messages[0]
    assert isinstance(body, (tuple, list))
    assert body[0] == ()
    assert body[1] == {"operation_id": str(operation)}
    assert queue == "ai-interactive"
    observed = [
        json.dumps(messages, default=str),
        *(record.getMessage() for record in caplog.records),
    ]
    assert all(BODY.decode() not in value for value in observed)
    assert all(b64encode(BODY).decode() not in value for value in observed)
    assert all(scope.token not in value for value in observed)
