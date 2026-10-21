"""Uploader provenance is never a grant after current source authority is lost."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import ProfessionalRegistration, RoleGrant, UserClinicRole
from apps.orders.access import current_version
from apps.orders.models import (
    ResultAcknowledgment,
    ResultMatch,
    ResultQuarantine,
    ResultRelease,
    ResultVersion,
    ResultWorkItem,
)
from apps.orders.services import (
    OrdersAccessDeniedError,
    acknowledge_result,
    ingest_result,
    release_result,
)
from django.db import connection
from django.utils import timezone

from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from orders.support import ordered, submission

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "loss",
    ["role_revoked", "moved_clinic", "permission_narrowed", "registration_revoked"],
)
@pytest.mark.parametrize("quarantined", [False, True])
def test_former_uploader_cannot_read_after_current_authority_loss(
    rbac_graph: RbacGraph, loss: str, *, quarantined: bool
) -> None:
    graph = rbac_graph
    world = ordered(graph)
    source = submission(world)
    if quarantined:
        source = replace(
            source, identity=replace(world.identity, name="Pessoa SINTETICA diferente")
        )
    with as_actor(graph, graph.physician):
        receipt = ingest_result(clinic_id=graph.clinic_a, submission=source)
        assert receipt.state == ("quarantined" if quarantined else "matched")
        assert ResultVersion.objects.filter(pk=receipt.version_id).count() == 1
        if not quarantined:
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
    now = timezone.now()
    with owner_context(graph.organization_a):
        if loss in {"role_revoked", "moved_clinic"}:
            UserClinicRole.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_b,
                user_id=graph.physician,
                role="physician" if loss == "moved_clinic" else "finance",
            )
            UserClinicRole.objects.filter(
                clinic_id=graph.clinic_a, user_id=graph.physician
            ).update(revoked_at=now)
        elif loss == "permission_narrowed":
            RoleGrant.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                role="physician",
                permission="result.read",
                valid_from=now - timedelta(minutes=1),
            )
        else:
            ProfessionalRegistration.objects.filter(
                clinic_id=graph.clinic_a, user_id=graph.physician
            ).update(revoked_at=now)
    # Organization access intentionally survives every authority change.
    with as_actor(graph, graph.physician), connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.has_permission('order.route',%s,NULL),"
            "clinic_app.has_permission('result.read',%s,%s)",
            [graph.clinic_a, graph.clinic_a, world.enrollment],
        )
        assert cursor.fetchone() == (False, False)
        cursor.execute(
            "SELECT clinic_app.orders_can_read_version(%s),"
            "clinic_app.orders_can_read_version(%s)",
            [receipt.version_id, uuid4()],
        )
        assert cursor.fetchone() == (False, False)
        assert not ResultVersion.objects.filter(pk=receipt.version_id).exists()
        for model in (
            ResultMatch,
            ResultQuarantine,
            ResultAcknowledgment,
            ResultRelease,
            ResultWorkItem,
        ):
            assert not model.objects.filter(version_id=receipt.version_id).exists()
        denials = []
        for clinic, version in (
            (graph.clinic_a, receipt.version_id),
            (graph.clinic_a, uuid4()),
            (graph.clinic_b, receipt.version_id),
        ):
            with pytest.raises(OrdersAccessDeniedError) as denied:
                current_version(clinic_id=clinic, version_id=version)
            denials.append(denied.value.args)
        assert denials == [("current actor unauthorized",)] * 3
