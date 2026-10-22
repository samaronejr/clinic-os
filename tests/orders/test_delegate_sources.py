"""A patient's release never silently grants a representative source access."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.delegates import revoke, switch_delegate
from apps.intake.models import Patient
from apps.intake.patient_access import patient_session_context
from apps.orders.queries import patient_sources
from apps.orders.services import (
    IdentityEvidence,
    acknowledge_result,
    ingest_result,
    place_order,
    release_result,
)
from apps.orders.storage import source_bytes

from ehr.longitudinal_support import chart_world
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context, permission_context
from orders.support import SOURCE, World, submission
from patient_service_support import runtime_role
from patients.delegate_support import NotesFixture, active_grant, delegate_world

if TYPE_CHECKING:
    from pathlib import Path

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_delegate_requires_its_exact_release_and_revocation_hides_source(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    graph = rbac_graph
    chart = chart_world(graph)
    with owner_context(graph.organization_a):
        patient = Patient.objects.get(pk=chart.patient)
        identity = IdentityEvidence(
            birth_date=patient.birth_date, name=patient.full_name
        )
        notes = NotesFixture(
            chart.enrollment,
            chart.patient,
            chart.version.document.encounter_id,
            chart.version.pk,
        )
    delegate = delegate_world(graph, tmp_path, notes=notes)
    grant = active_grant(graph, delegate)
    with as_actor(graph, graph.physician):
        order = place_order(
            clinic_id=graph.clinic_a,
            enrollment_id=chart.enrollment,
            exam_codes=("cbc",),
            command_id=uuid4(),
        )
        receipt = ingest_result(
            clinic_id=graph.clinic_a,
            submission=submission(World(order, identity, chart.enrollment)),
        )
        acknowledge_result(
            clinic_id=graph.clinic_a,
            version_id=receipt.version_id,
            disposition="no_action",
        )
        release_result(
            clinic_id=graph.clinic_a,
            version_id=receipt.version_id,
            audience="patient",
        )
    with runtime_role(), patient_session_context(delegate.session_id):
        switch_delegate(clinic_id=graph.clinic_a, grant_id=grant.grant_id)
    with runtime_role(), patient_session_context(delegate.session_id):
        assert not patient_sources(version_id=receipt.version_id)
    with as_actor(graph, graph.physician):
        release_result(
            clinic_id=graph.clinic_a,
            version_id=receipt.version_id,
            audience="delegate",
            delegate_grant_id=grant.grant_id,
        )
    with runtime_role(), patient_session_context(delegate.session_id):
        sources = patient_sources(version_id=receipt.version_id)
        assert len(sources) == 1
        assert sources[0].version_id == receipt.version_id
        assert source_bytes(sources[0]) == SOURCE
    with permission_context(graph, delegate.verifier_id):
        revoke(
            clinic_id=graph.clinic_a,
            grant_id=grant.grant_id,
            expected_revision=grant.revision,
        )
    with runtime_role(), patient_session_context(delegate.session_id) as binding:
        assert binding is None
        assert not patient_sources(version_id=receipt.version_id)
