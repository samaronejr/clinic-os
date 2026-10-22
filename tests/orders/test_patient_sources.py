"""Patient visibility is an exact current-version audience grant, not a flag."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.intake.patient_access import patient_session_context
from apps.orders.queries import patient_sources
from apps.orders.services import (
    acknowledge_result,
    ingest_result,
    release_result,
)
from apps.orders.storage import source_bytes

from ehr.test_autosave import as_actor
from orders.support import SOURCE, ordered, submission
from patient_service_support import runtime_role
from renewal.test_patient_access import _issue, _redeem

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_patient_sources_require_current_acknowledgment_and_exact_patient_release(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    world = ordered(graph)
    original = submission(world)
    with as_actor(graph, graph.physician):
        first = ingest_result(clinic_id=graph.clinic_a, submission=original)
        assert not patient_sources()
    _, secret = _issue(graph, world.enrollment)
    session = _redeem(graph.clinic_a, secret)
    assert session is not None
    with runtime_role(), patient_session_context(session):
        assert not patient_sources(version_id=first.version_id)
    with as_actor(graph, graph.physician):
        acknowledge_result(
            clinic_id=graph.clinic_a,
            version_id=first.version_id,
            disposition="no_action",
        )
        release_result(
            clinic_id=graph.clinic_a,
            version_id=first.version_id,
            audience="patient",
        )
    with runtime_role(), patient_session_context(session):
        sources = patient_sources(version_id=first.version_id)
        assert len(sources) == 1
        assert sources[0].version_id == first.version_id
        assert source_bytes(sources[0]) == SOURCE
    with as_actor(graph, graph.physician):
        corrected = ingest_result(
            clinic_id=graph.clinic_a,
            submission=replace(
                original,
                result_id=first.result_id,
                predecessor_id=first.version_id,
                event_id=uuid4(),
                data=SOURCE + b"\ncorrected",
            ),
        )
    with runtime_role(), patient_session_context(session):
        assert not patient_sources(version_id=first.version_id)
        assert not patient_sources(version_id=corrected.version_id)
