"""Sentinel checks on actual gateway logs and returned output representations."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from apps.ai.artifacts import read_artifact
from apps.ai.models import AIArtifact
from apps.core.integration import execute_operation
from apps.tenancy.db import tenant_context

from ai.support import SENTINEL, enqueue, seed
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_gateway_logs_contain_neither_input_nor_output_content(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given: a real offline gateway call and DEBUG-level application capture.
    world = seed(rbac_graph, monkeypatch)
    caplog.set_level(logging.DEBUG)
    result = enqueue(world)
    assert result.operation_id is not None
    # When: the real outbox executes and the authorized user reads the result.
    with runtime_role():
        assert execute_operation(result.operation_id) == "succeeded"
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        output = AIArtifact.objects.get(invocation_id=result.invocation_id)
        content = read_artifact(clinic_id=rbac_graph.clinic_a, artifact_id=output.pk)
    # Then: neither plaintext source nor provider output reaches a log record.
    assert SENTINEL not in caplog.text
    assert content not in caplog.text
