"""Verified patient routing replaces an uploader's historical proposed scope."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import CareTeamMembership
from apps.intake.models import PatientClinicEnrollment
from apps.orders.access import current_version
from apps.orders.models import (
    ResultAcknowledgment,
    ResultMatch,
    ResultObservation,
    ResultQuarantine,
    ResultRelease,
    ResultVersion,
    ResultWorkItem,
)
from apps.orders.services import (
    IdentityEvidence,
    IngestReceipt,
    ObservationInput,
    OrdersAccessDeniedError,
    OrdersConflictError,
    ResultInput,
    acknowledge_result,
    ingest_result,
    match_result,
    place_order,
    release_result,
)
from apps.orders.storage import source_bytes
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)
from orders.support import World, ordered, submission

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@dataclass(frozen=True, slots=True)
class RematchedSource:
    proposed: World
    matched: World
    reviewer: UUID
    source: ResultInput
    receipt: IngestReceipt


def rematched_source(graph: RbacGraph) -> RematchedSource:
    proposed = ordered(graph)
    reviewer, enrollment = permission_actor(graph, "physician")
    now = timezone.now()
    with owner_context(graph.organization_a):
        patient = (
            PatientClinicEnrollment.objects.select_related("patient")
            .get(pk=enrollment)
            .patient
        )
        identity = IdentityEvidence(
            birth_date=patient.birth_date, name=patient.full_name
        )
        assignment = CareTeamMembership.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            patient_enrollment_id=enrollment,
            user_id=graph.physician,
            role="physician",
            valid_from=now - timedelta(days=1),
            valid_to=now + timedelta(days=1),
        )
    with permission_context(graph, reviewer):
        order = place_order(
            clinic_id=graph.clinic_a,
            enrollment_id=enrollment,
            exam_codes=("cbc",),
            command_id=uuid4(),
        )
    matched = World(order, identity, enrollment)
    source = replace(
        submission(proposed),
        identity=identity,
        observations=(ObservationInput(code="cbc", value="7,10", unit="source-unit"),),
    )
    with permission_context(graph, graph.physician):
        receipt = ingest_result(clinic_id=graph.clinic_a, submission=source)
        assert receipt.state == "quarantined"
    with permission_context(graph, graph.shared_user):
        assert (
            match_result(
                clinic_id=graph.clinic_a,
                version_id=receipt.version_id,
                order_id=order.pk,
            )
            == "matched"
        )
    with permission_context(graph, reviewer):
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
    with owner_context(graph.organization_a):
        CareTeamMembership.objects.filter(pk=assignment.pk).update(revoked_at=now)
    for actor, expected in (
        (graph.physician, (False, True, False)),
        (reviewer, (False, False, True)),
    ):
        with permission_context(graph, actor), connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.has_permission('order.route',%s,NULL),"
                "clinic_app.has_permission('result.read',%s,%s),"
                "clinic_app.has_permission('result.read',%s,%s)",
                [
                    graph.clinic_a,
                    graph.clinic_a,
                    proposed.enrollment,
                    graph.clinic_a,
                    enrollment,
                ],
            )
            assert cursor.fetchone() == expected
    return RematchedSource(proposed, matched, reviewer, source, receipt)


def test_proposed_patient_authority_cannot_read_a_different_matched_patient(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    world = rematched_source(graph)
    with permission_context(graph, graph.physician), connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.orders_can_read_version(%s),"
            "clinic_app.orders_can_read_version(%s)",
            [world.receipt.version_id, uuid4()],
        )
        assert cursor.fetchone() == (False, False)
        assert not ResultVersion.objects.filter(pk=world.receipt.version_id).exists()
        for model in (
            ResultMatch,
            ResultObservation,
            ResultQuarantine,
            ResultAcknowledgment,
            ResultRelease,
            ResultWorkItem,
        ):
            assert not model.objects.filter(
                version_id=world.receipt.version_id
            ).exists()
        denials = []
        for clinic, version in (
            (graph.clinic_a, world.receipt.version_id),
            (graph.clinic_a, uuid4()),
            (graph.clinic_b, world.receipt.version_id),
        ):
            with pytest.raises(OrdersAccessDeniedError) as denied:
                current_version(clinic_id=clinic, version_id=version)
            denials.append(denied.value.args)
        assert denials == [("current actor unauthorized",)] * 3


def test_matched_patient_reader_keeps_exact_source_access(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    world = rematched_source(graph)
    with permission_context(graph, world.reviewer), connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.orders_can_read_version(%s)",
            [world.receipt.version_id],
        )
        assert cursor.fetchone() == (True,)
        version = current_version(
            clinic_id=graph.clinic_a, version_id=world.receipt.version_id
        )
        assert version.proposed_order_id == world.proposed.order.pk
        assert (
            ResultMatch.objects.get(version=version).order_id == world.matched.order.pk
        )
        assert source_bytes(version) == world.source.data
        assert version.observations.get().source["value"] == "7,10"
        assert ResultAcknowledgment.objects.filter(version=version).count() == 1
        assert ResultRelease.objects.filter(version=version).count() == 1


def test_stale_proposed_scope_cannot_supersede_a_rematched_source(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    world = rematched_source(graph)
    with (
        permission_context(graph, graph.physician),
        pytest.raises(OrdersConflictError, match="source_superseded"),
    ):
        ingest_result(
            clinic_id=graph.clinic_a,
            submission=replace(
                world.source,
                event_id=uuid4(),
                result_id=world.receipt.result_id,
                predecessor_id=world.receipt.version_id,
            ),
        )
    with owner_context(graph.organization_a):
        assert (
            ResultVersion.objects.filter(result_id=world.receipt.result_id).count() == 1
        )


def test_matched_patient_clinician_can_correct_without_inheriting_review(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    world = rematched_source(graph)
    with permission_context(graph, world.reviewer):
        corrected = ingest_result(
            clinic_id=graph.clinic_a,
            submission=replace(
                world.source,
                order_id=world.matched.order.pk,
                event_id=uuid4(),
                data=world.source.data + b"\ncorrected",
                result_id=world.receipt.result_id,
                predecessor_id=world.receipt.version_id,
            ),
        )
        assert corrected.number == 2
        assert corrected.state == "matched"
        assert (
            ResultMatch.objects.get(version_id=corrected.version_id).order_id
            == world.matched.order.pk
        )
        assert not ResultAcknowledgment.objects.filter(
            version_id=corrected.version_id
        ).exists()
        assert not ResultRelease.objects.filter(
            version_id=corrected.version_id
        ).exists()
        assert (
            source_bytes(
                current_version(
                    clinic_id=graph.clinic_a, version_id=world.receipt.version_id
                )
            )
            == world.source.data
        )


def test_stale_proposed_scope_cannot_replay_a_rematched_source(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    world = rematched_source(graph)
    with (
        permission_context(graph, graph.physician),
        pytest.raises(OrdersAccessDeniedError, match="current actor unauthorized"),
    ):
        ingest_result(clinic_id=graph.clinic_a, submission=world.source)
    with permission_context(graph, world.reviewer), connection.cursor() as cursor:
        cursor.execute(
            "SELECT version_id FROM clinic_app.orders_event_receipt(%s,%s)",
            [graph.clinic_a, world.source.event_id],
        )
        assert cursor.fetchone() == (world.receipt.version_id,)


def test_raw_correction_guard_rejects_the_historical_proposed_patient(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    world = rematched_source(graph)
    with owner_context(graph.organization_a):
        previous = ResultVersion.objects.get(pk=world.receipt.version_id)
    with (
        permission_context(graph, graph.physician),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        ResultVersion.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            result_id=world.receipt.result_id,
            number=2,
            predecessor_id=world.receipt.version_id,
            event_id=uuid4(),
            fingerprint="a" * 64,
            proposed_order_id=world.proposed.order.pk,
            identity=world.source.identity.payload(),
            storage_key="b" * 64,
            sha256=previous.sha256,
            size_bytes=previous.size_bytes,
            content_type=previous.content_type,
            uploader_id=graph.physician,
        )
    with owner_context(graph.organization_a):
        assert (
            ResultVersion.objects.filter(result_id=world.receipt.result_id).count() == 1
        )
