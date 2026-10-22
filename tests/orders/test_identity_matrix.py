"""Independent identity evidence, operator resolution and conflicting factors."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from apps.intake.demographics import add_identifier
from apps.orders.models import ResultMatch, ResultQuarantine
from apps.orders.services import ingest_result, match_result

from ehr.test_autosave import as_actor
from orders.support import ordered, submission

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    ("named", "identifier", "birthday", "state"),
    [
        ("", "SINTETICO-31", True, "matched"),
        ("exact", "SINTETICO-31", True, "matched"),
        ("exact", "SINTETICO-OUTRO", True, "quarantined"),
        ("wrong", "SINTETICO-31", True, "quarantined"),
        ("", "SINTETICO-31", False, "quarantined"),
    ],
)
def test_dob_and_identifier_never_override_an_explicit_conflicting_factor(
    rbac_graph: RbacGraph,
    named: str,
    identifier: str,
    birthday: bool,
    state: str,
) -> None:
    world = ordered(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.shared_user):
        add_identifier(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=world.enrollment,
            kind="other",
            value="SINTETICO-31",
        )
    identity = replace(
        world.identity,
        name=world.identity.name if named == "exact" else named,
        birth_date=world.identity.birth_date if birthday else None,
        identifier_kind="other",
        identifier=identifier,
    )
    with as_actor(rbac_graph, rbac_graph.physician):
        receipt = ingest_result(
            clinic_id=rbac_graph.clinic_a,
            submission=replace(submission(world), identity=identity),
        )
        assert receipt.state == state
        assert ResultMatch.objects.filter(version_id=receipt.version_id).exists() == (
            state == "matched"
        )


def test_unknown_order_can_be_resolved_only_against_immutable_source_evidence(
    rbac_graph: RbacGraph,
) -> None:
    world = ordered(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.shared_user):
        receipt = ingest_result(
            clinic_id=rbac_graph.clinic_a,
            submission=replace(submission(world), order_id=None),
        )
        assert receipt.state == "quarantined"
        assert (
            ResultQuarantine.objects.get(version_id=receipt.version_id).reason
            == "unknown_order"
        )
    with as_actor(rbac_graph, rbac_graph.shared_user):
        assert (
            match_result(
                clinic_id=rbac_graph.clinic_a,
                version_id=receipt.version_id,
                order_id=world.order.pk,
            )
            == "matched"
        )
