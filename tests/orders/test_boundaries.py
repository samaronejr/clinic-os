"""Adversarial scope, source, replay and matching-factor partitions."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.orders.models import ResultQuarantine, ResultVersion
from apps.orders.queries import result_rows
from apps.orders.services import (
    IdentityEvidence,
    OrdersAccessDeniedError,
    OrdersConflictError,
    ingest_result,
)
from django.core.exceptions import ValidationError

from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from orders.support import ordered, submission

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    ("missing", "reason"),
    [("date", "ambiguous"), ("name", "ambiguous"), ("both", "ambiguous")],
)
def test_one_or_zero_identity_factors_are_quarantined(
    rbac_graph: RbacGraph,
    missing: str,
    reason: str,
) -> None:
    world = ordered(rbac_graph)
    identity = IdentityEvidence(
        birth_date=None if missing in {"date", "both"} else world.identity.birth_date,
        name="" if missing in {"name", "both"} else world.identity.name,
    )
    with as_actor(rbac_graph, rbac_graph.physician):
        receipt = ingest_result(
            clinic_id=rbac_graph.clinic_a,
            submission=replace(submission(world), identity=identity),
        )
        assert not result_rows(
            clinic_id=rbac_graph.clinic_a, enrollment_id=world.enrollment
        )
        assert (
            ResultQuarantine.objects.get(version_id=receipt.version_id).reason == reason
        )


@pytest.mark.parametrize("factor", ["date", "name"])
def test_wrong_identity_factor_never_files_a_source(
    rbac_graph: RbacGraph,
    factor: str,
) -> None:
    world = ordered(rbac_graph)
    identity = replace(
        world.identity,
        **(
            {"birth_date": date(1901, 1, 1)}
            if factor == "date"
            else {"name": world.identity.name + " X"}
        ),
    )
    with as_actor(rbac_graph, rbac_graph.physician):
        receipt = ingest_result(
            clinic_id=rbac_graph.clinic_a,
            submission=replace(submission(world), identity=identity),
        )
        assert receipt.state == "quarantined"
        assert not result_rows(
            clinic_id=rbac_graph.clinic_a, enrollment_id=world.enrollment
        )


def test_case_and_whitespace_normalization_is_not_fuzzy_matching(
    rbac_graph: RbacGraph,
) -> None:
    world = ordered(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.physician):
        receipt = ingest_result(
            clinic_id=rbac_graph.clinic_a,
            submission=replace(
                submission(world),
                identity=replace(
                    world.identity,
                    name="  " + world.identity.name.upper().replace(" ", "   ") + "  ",
                ),
            ),
        )
    assert receipt.state == "matched"


@pytest.mark.parametrize("foreign", [False, True])
def test_cross_tenant_ingest_has_identical_payload_free_denial(
    rbac_graph: RbacGraph,
    foreign: bool,
) -> None:
    world = ordered(rbac_graph)
    with (
        as_actor(rbac_graph, rbac_graph.physician),
        pytest.raises(OrdersAccessDeniedError, match=r"^current actor unauthorized$"),
    ):
        ingest_result(
            clinic_id=rbac_graph.clinic_c if foreign else uuid4(),
            submission=submission(world),
        )
    with owner_context(rbac_graph.organization_a):
        assert not ResultVersion.objects.exists()


@pytest.mark.parametrize(
    "data",
    [b"<script>bad</script>", b"PK\x03\x04archive", b"%PDF-1.4\n/JavaScript", b""],
)
def test_unsafe_source_is_refused_without_any_source_row(
    rbac_graph: RbacGraph,
    data: bytes,
) -> None:
    world = ordered(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.physician), pytest.raises(ValidationError):
        ingest_result(
            clinic_id=rbac_graph.clinic_a,
            submission=replace(submission(world), data=data),
        )
    with owner_context(rbac_graph.organization_a):
        assert not ResultVersion.objects.exists()


def test_altered_event_replay_has_no_second_effect(rbac_graph: RbacGraph) -> None:
    world = ordered(rbac_graph)
    source = submission(world)
    with as_actor(rbac_graph, rbac_graph.physician):
        ingest_result(clinic_id=rbac_graph.clinic_a, submission=source)
        with pytest.raises(OrdersConflictError, match="event_mismatch"):
            ingest_result(
                clinic_id=rbac_graph.clinic_a,
                submission=replace(source, data=source.data + b"\nchanged"),
            )
        assert ResultVersion.objects.count() == 1
