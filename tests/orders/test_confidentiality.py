"""Clinical source codes and values must not become routing-role projections."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from apps.identity.current_context import CurrentActorError, require_permission
from apps.orders.models import ResultObservation
from apps.orders.services import ObservationInput, ingest_result
from django.db import connection

from ehr.test_autosave import as_actor
from orders.support import ordered, submission

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_routing_upload_does_not_grant_structured_clinical_read(
    rbac_graph: RbacGraph,
) -> None:
    world = ordered(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.shared_user):
        with pytest.raises(CurrentActorError):
            require_permission(
                "result.read",
                clinic_id=rbac_graph.clinic_a,
                patient_enrollment_id=world.enrollment,
            )
        receipt = ingest_result(
            clinic_id=rbac_graph.clinic_a,
            submission=replace(
                submission(world),
                observations=(
                    ObservationInput(
                        code="analito_sintetico", value="7,10", unit="source-unit"
                    ),
                ),
            ),
        )
        assert receipt.state == "matched"
        assert not ResultObservation.objects.filter(
            version_id=receipt.version_id
        ).exists()


def test_observation_has_no_plaintext_shadow_of_the_encrypted_report_code() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='clinic_app' AND table_name='orders_resultobservation'"
        )
        assert "code" not in {row[0] for row in cursor.fetchall()}
