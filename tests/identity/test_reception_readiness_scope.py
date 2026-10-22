"""Queue capability alone cannot expose arrival-readiness metadata."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, TypedDict

import pytest
from apps.identity.models import UserClinicRole
from apps.tenancy.db import tenant_context
from django.db import connection

from auth.stepup_test_support import create_role_actor
from identity.legacy_parity_support import world
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


class ReadinessObservation(TypedDict):
    queue: bool
    move: bool
    own: bool
    admitted: bool


def _observe(graph: RbacGraph, actor: UUID, appointment: UUID) -> ReadinessObservation:
    with (
        runtime_role(),
        tenant_context(actor, graph.organization_a),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT clinic_app.has_permission('reception.queue',%s,NULL), "
            "clinic_app.has_permission('appointment.move',%s,NULL), "
            "clinic_app.has_permission('appointment.move_own',%s,NULL)",
            [graph.clinic_a, graph.clinic_a, graph.clinic_a],
        )
        permissions = cursor.fetchone()
        assert permissions is not None
        cursor.execute(
            "SELECT * FROM clinic_app.reception_readiness(%s,%s)",
            [graph.clinic_a, appointment],
        )
        return {
            "queue": permissions[0],
            "move": permissions[1],
            "own": permissions[2],
            "admitted": bool(cursor.fetchall()),
        }


def test_queue_only_nurse_cannot_read_arrival_metadata(
    rbac_graph: RbacGraph, record_property: Callable[[str, object], None]
) -> None:
    subject = world(rbac_graph, "physician")
    assert subject.appointment.practitioner_id == subject.actor.pk
    nurse = create_role_actor(rbac_graph, UserClinicRole.Role.NURSE)
    manager = create_role_actor(rbac_graph, UserClinicRole.Role.CLINIC_ADMIN)
    results = {
        label: _observe(rbac_graph, actor, subject.appointment.pk)
        for label, actor in (
            ("queue_only_nurse", nurse.pk),
            ("existing_move", manager.pk),
            ("existing_own_clinician", subject.actor.pk),
        )
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_functiondef("
            "'clinic_app.reception_readiness(uuid,uuid)'::regprocedure)"
        )
        row = cursor.fetchone()
    assert row is not None
    record_property(
        "readiness_installed_definition_sha256",
        hashlib.sha256(row[0].encode()).hexdigest(),
    )
    record_property("readiness_authority_results", json.dumps(results, sort_keys=True))
    assert results["queue_only_nurse"]["queue"] is True
    assert results["queue_only_nurse"]["move"] is False
    assert results["queue_only_nurse"]["own"] is False
    assert results["existing_move"]["queue"] is False
    assert results["existing_move"]["move"] is True
    assert results["existing_own_clinician"]["move"] is False
    assert results["existing_own_clinician"]["own"] is True
    assert results["existing_move"]["admitted"] is True
    assert results["existing_own_clinician"]["admitted"] is True
    assert results["queue_only_nurse"]["admitted"] is False
