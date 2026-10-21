"""Version fencing prevents corrections from inheriting an earlier review."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.orders.models import (
    ResultAcknowledgment,
    ResultRelease,
    ResultVersion,
    ResultWorkItem,
)
from apps.orders.services import (
    ObservationInput,
    OrdersAccessDeniedError,
    OrdersConflictError,
    acknowledge_result,
    ingest_result,
    release_result,
)
from django.db import DatabaseError, transaction

from ehr.test_autosave import as_actor
from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)
from orders.support import ordered, submission

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_correction_retains_old_bytes_but_resets_acknowledgment_and_release(
    rbac_graph: RbacGraph,
) -> None:
    world = ordered(rbac_graph)
    first = submission(world)
    with as_actor(rbac_graph, rbac_graph.physician):
        receipt = ingest_result(clinic_id=rbac_graph.clinic_a, submission=first)
        acknowledge_result(
            clinic_id=rbac_graph.clinic_a,
            version_id=receipt.version_id,
            disposition="no_action",
        )
        release_result(
            clinic_id=rbac_graph.clinic_a,
            version_id=receipt.version_id,
            audience="patient",
        )
        # When: a new source explicitly supersedes that exact version.
        corrected = ingest_result(
            clinic_id=rbac_graph.clinic_a,
            submission=replace(
                first,
                event_id=uuid4(),
                data=first.data + b"\ncorrected",
                result_id=receipt.result_id,
                predecessor_id=receipt.version_id,
            ),
        )
        # Then: historical receipts survive; none applies to the correction.
        assert corrected.number == 2
        assert corrected.state == "matched"
        assert (
            ResultAcknowledgment.objects.filter(version_id=receipt.version_id).count()
            == 1
        )
        assert not ResultAcknowledgment.objects.filter(
            version_id=corrected.version_id
        ).exists()
        assert not ResultRelease.objects.filter(
            version_id=corrected.version_id
        ).exists()
        with pytest.raises(OrdersConflictError, match="source_superseded"):
            release_result(
                clinic_id=rbac_graph.clinic_a,
                version_id=receipt.version_id,
                audience="patient",
            )
        with pytest.raises(OrdersConflictError, match="acknowledgment_required"):
            release_result(
                clinic_id=rbac_graph.clinic_a,
                version_id=corrected.version_id,
                audience="patient",
            )


def test_source_critical_flag_creates_one_urgent_owned_task_before_release(
    rbac_graph: RbacGraph,
) -> None:
    world = ordered(rbac_graph)
    source = replace(
        submission(world),
        observations=(
            ObservationInput(
                code="cbc", value="7,10", unit="source-unit", abnormal_flag="critical"
            ),
        ),
    )
    with as_actor(rbac_graph, rbac_graph.physician):
        receipt = ingest_result(clinic_id=rbac_graph.clinic_a, submission=source)
        replay = ingest_result(clinic_id=rbac_graph.clinic_a, submission=source)
        assert receipt.version_id == replay.version_id
    with owner_context(rbac_graph.organization_a):
        item = ResultWorkItem.objects.get(version_id=receipt.version_id, kind="review")
        assert item.task.priority == "urgent"
        assert item.task.owner_user_id == rbac_graph.physician
        assert item.task.created_by_id is None
        assert ResultWorkItem.objects.filter(version_id=receipt.version_id).count() == 1
        assert not ResultRelease.objects.filter(version_id=receipt.version_id).exists()
        assert item.version.observations.get().source["value"] == "7,10"


@pytest.mark.parametrize("role", ["receptionist", "nurse", "clinic_admin", "finance"])
def test_nonclinical_actor_cannot_acknowledge_or_release(
    rbac_graph: RbacGraph,
    role: str,
) -> None:
    world = ordered(rbac_graph)
    actor, _ = permission_actor(rbac_graph, role)
    with as_actor(rbac_graph, rbac_graph.physician):
        receipt = ingest_result(
            clinic_id=rbac_graph.clinic_a, submission=submission(world)
        )
    with permission_context(rbac_graph, actor), pytest.raises(OrdersAccessDeniedError):
        acknowledge_result(
            clinic_id=rbac_graph.clinic_a,
            version_id=receipt.version_id,
            disposition="no_action",
        )
    with permission_context(rbac_graph, actor), pytest.raises(OrdersAccessDeniedError):
        release_result(
            clinic_id=rbac_graph.clinic_a,
            version_id=receipt.version_id,
            audience="patient",
        )
    with owner_context(rbac_graph.organization_a):
        assert not ResultAcknowledgment.objects.filter(
            version_id=receipt.version_id
        ).exists()


def test_source_rows_refuse_raw_updates_and_deletes(rbac_graph: RbacGraph) -> None:
    world = ordered(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.physician):
        receipt = ingest_result(
            clinic_id=rbac_graph.clinic_a, submission=submission(world)
        )
    # Then: even the owner cannot erase or rewrite source history with ordinary DML.
    with owner_context(rbac_graph.organization_a):
        with pytest.raises(DatabaseError), transaction.atomic():
            ResultVersion.objects.filter(pk=receipt.version_id).update(sha256="0" * 64)
        with pytest.raises(DatabaseError), transaction.atomic():
            ResultVersion.objects.filter(pk=receipt.version_id)._raw_delete("default")
