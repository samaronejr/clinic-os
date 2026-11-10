"""Exact interop catalog posture and fail-closed tenant reads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from apps.interop.models import ApiAccessToken, ApiClient, ApiClientSecret
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction

from identity.permission_support import permission_context
from interop.catalog import discover_catalog
from interop.support import access_token, local_bucket, make_client, org_admin
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from interop.support import LocalBucket
    from rbac_fixtures import RbacGraph

__all__ = ("local_bucket",)
pytestmark = pytest.mark.django_db(transaction=True)


def test_exact_catalog_is_the_discovered_force_rls_grant_and_trigger_snapshot() -> None:
    assert discover_catalog() == json.loads(
        Path(__file__).with_name("posture.json").read_text()
    )


def test_missing_foreign_and_clinic_scoped_contexts_expose_no_partner_rows(
    rbac_graph: RbacGraph, local_bucket: LocalBucket
) -> None:
    graph = rbac_graph
    admin = org_admin(graph)
    access_token(make_client(graph, admin))
    with runtime_role(), transaction.atomic():
        assert not ApiClient.objects.exists()
    with runtime_role(), tenant_context(admin, graph.organization_a):
        assert ApiClient.objects.count() == 1
    # Staff of the same organization without organization authority see nothing.
    for actor in (graph.physician, graph.clinic_admin, graph.shared_user):
        with permission_context(graph, actor):
            assert not ApiClient.objects.exists()
    with permission_context(graph, admin):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant',%s,true)",
                [str(graph.organization_b)],
            )
        assert not ApiClient.objects.exists()


@pytest.mark.parametrize("model", [ApiClientSecret, ApiAccessToken])
def test_runtime_role_holds_no_privilege_on_credential_digests(
    rbac_graph: RbacGraph, model: type[ApiClientSecret | ApiAccessToken]
) -> None:
    graph = rbac_graph
    admin = org_admin(graph)
    with (
        runtime_role(),
        tenant_context(admin, graph.organization_a),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        list(model.objects.values_list("pk", flat=True))
