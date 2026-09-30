"""Real clinic_agent login adapters for the machine-principal boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from apps.identity.models import ServicePrincipal, ServicePrincipalGrant
from apps.tenancy.db import ServicePrincipalAccessDeniedError, service_principal_context
from django.db import connections

from identity.nonstaff_differential import DifferentialProbe
from identity.permission_support import owner_context

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

SYMBOL = "apps.tenancy.db.service_principal_context"


def seed_principal(graph: RbacGraph) -> ServicePrincipal:
    """One granted principal bound to the real clinic_agent login in clinic A."""
    with owner_context(graph.organization_a):
        principal = ServicePrincipal.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            name="sintetico-census",
            db_identity="clinic_agent",
            purpose="availability",
        )
        ServicePrincipalGrant.objects.create(
            organization_id=graph.organization_a,
            principal=principal,
            permission="appointment.read",
            subject_scope="clinic",
        )
    return principal


def _enter(principal_id: UUID, clinic_id: UUID) -> bool:
    try:
        with service_principal_context(principal_id=principal_id, clinic_id=clinic_id):
            return True
    except ServicePrincipalAccessDeniedError:
        return False


def principal_probes(graph: RbacGraph) -> list[DifferentialProbe]:
    principal = seed_principal(graph)
    # Trusted preparation: the login handshake is not the observed boundary.
    connections["agent"].ensure_connection()
    return [
        DifferentialProbe(
            SYMBOL, lambda: _enter(principal.pk, graph.clinic_a), owns_transaction=True
        ),
        DifferentialProbe(
            SYMBOL,
            lambda: _enter(principal.pk, graph.clinic_b),
            expected=False,
            owns_transaction=True,
        ),
        DifferentialProbe(
            SYMBOL,
            lambda: _enter(uuid4(), graph.clinic_a),
            expected=False,
            owns_transaction=True,
        ),
    ]
