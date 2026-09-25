"""Shared cross-tenant probe helpers for domain tests.

``tenant_probe_pair`` builds the standard two-organization graph through
``rbac_fixtures.rbac_graph`` (organizations A/B, clinics a/b/c, a shared
user holding memberships in both organizations). ``assert_no_cross_tenant_rows``
then proves that a model's rows from one organization are invisible to the
runtime role inside the other organization's tenant context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.tenancy.db import tenant_context

from patient_service_support import runtime_role

if TYPE_CHECKING:
    from django.db.models import Model

    from rbac_fixtures import RbacGraph

__all__ = ["assert_no_cross_tenant_rows", "tenant_probe_pair"]


@pytest.fixture
def tenant_probe_pair(rbac_graph: RbacGraph) -> RbacGraph:
    """Return the shared two-organization graph for cross-tenant probes."""
    return rbac_graph


def assert_no_cross_tenant_rows(graph: RbacGraph, model: type[Model]) -> None:
    """Assert ``model`` exposes zero cross-tenant rows under ``clinic_app``.

    Runs as the runtime role inside ``tenant_context`` for each
    organization of ``graph`` and counts rows owned by the other
    organization; any visible row fails. The probe always fails when the
    organization owns no rows at all, so the assertion can never pass
    vacuously on an empty table: callers must seed rows first.
    """
    pairs = (
        (graph.organization_a, graph.organization_b),
        (graph.organization_b, graph.organization_a),
    )
    for own_org, other_org in pairs:
        with runtime_role(), tenant_context(graph.shared_user, own_org):
            own_rows = model._default_manager.filter(organization_id=own_org).count()
            assert own_rows > 0, (
                f"{model._meta.db_table}: probe is vacuous, "
                "the organization owns no rows"
            )
            cross_tenant = model._default_manager.filter(
                organization_id=other_org
            ).count()
            assert cross_tenant == 0, (
                f"{model._meta.db_table}: {cross_tenant} rows from another "
                "organization are visible to the runtime role"
            )
