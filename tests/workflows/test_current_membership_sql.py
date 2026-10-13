"""Direct SQL oracles for retained-role revocation across cut-1 authorities."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.identity.models import UserClinicRole
from django.db import connection
from django.utils import timezone

from identity.permission_support import owner_context, permission_context

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "function",
    [
        "workflows_owned",
        "workflows_owner_valid",
        "workflows_staff_catalog",
        "scheduling_service_practitioners",
    ],
)
def test_sql_authority_excludes_retained_revoked_physician_membership(
    rbac_graph: RbacGraph, function: str
) -> None:
    graph = rbac_graph
    with owner_context(graph.organization_a):
        assert (
            UserClinicRole.objects.filter(
                clinic_id=graph.clinic_a, user_id=graph.shared_user
            ).update(workflow_bundle_version=2)
            == 1
        )
        membership = UserClinicRole.objects.get(
            clinic_id=graph.clinic_a,
            user_id=graph.physician,
            role=UserClinicRole.Role.PHYSICIAN,
        )
        if function == "workflows_owned":
            UserClinicRole.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                user_id=graph.physician,
                role=UserClinicRole.Role.RECEPTIONIST,
                workflow_bundle_version=2,
            )
    actor = graph.physician if function == "workflows_owned" else graph.shared_user
    query = {
        "workflows_owned": (
            "SELECT clinic_app.workflows_owned(%s, NULL, 'physician')",
            [graph.clinic_a],
        ),
        "workflows_owner_valid": (
            "SELECT clinic_app.workflows_owner_valid(%s, %s, '')",
            [graph.clinic_a, graph.physician],
        ),
        "workflows_staff_catalog": (
            "SELECT user_id FROM clinic_app.workflows_staff_catalog(%s)",
            [graph.clinic_a],
        ),
        "scheduling_service_practitioners": (
            "SELECT user_id FROM clinic_app.scheduling_service_practitioners(%s)",
            [graph.clinic_a],
        ),
    }[function]
    scalar = function in {"workflows_owned", "workflows_owner_valid"}
    with permission_context(graph, actor), connection.cursor() as cursor:
        if function == "workflows_owned":
            cursor.execute(
                "SELECT clinic_app.has_permission('tasks.view', %s, NULL)",
                [graph.clinic_a],
            )
            assert cursor.fetchone() == (True,)
        cursor.execute(*query)
        before = cursor.fetchall()
    if scalar:
        assert before == [(True,)]
    else:
        assert (graph.physician,) in before
    with owner_context(graph.organization_a):
        assert (
            UserClinicRole.objects.filter(pk=membership.pk).update(
                revoked_at=timezone.now()
            )
            == 1
        )
        assert UserClinicRole.all_objects.get(pk=membership.pk).revoked_at is not None
    # Bind the same GUCs without tenant-entry prechecks. The function itself
    # must close its grant, even when a former physician's session is stale.
    with permission_context(graph, actor), connection.cursor() as cursor:
        if function == "workflows_owned":
            cursor.execute(
                "SELECT clinic_app.has_permission('tasks.view', %s, NULL)",
                [graph.clinic_a],
            )
            assert cursor.fetchone() == (True,)
        cursor.execute(*query)
        after = cursor.fetchall()
    if scalar:
        assert after == [(False,)]
    else:
        assert (graph.physician,) not in after
