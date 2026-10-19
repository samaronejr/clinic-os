"""Real clinic_agent login adapters for the machine-principal boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from apps.identity.models import ServicePrincipal, ServicePrincipalGrant
from apps.scheduling.reception import issue_display_device
from apps.scheduling.reception_views import DEVICE_COOKIE, waiting_display
from apps.tenancy.db import (
    ServicePrincipalAccessDeniedError,
    service_principal_context,
    tenant_context,
)
from django.contrib.auth.models import AnonymousUser
from django.db import connections
from django.test import RequestFactory

from identity.nonstaff_differential import DifferentialProbe
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

SYMBOL = "apps.tenancy.db.service_principal_context"


def _display(token: str) -> bool:
    request = RequestFactory().get("/waiting-display/")
    request.user = AnonymousUser()
    request.COOKIES[DEVICE_COOKIE] = token
    return waiting_display(request).status_code == 200


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
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        device = issue_display_device(clinic_id=graph.clinic_a)
    # Trusted preparation: the login handshake is not the observed boundary.
    connections["agent"].ensure_connection()
    return [
        DifferentialProbe(
            "apps.scheduling.reception_views.waiting_display",
            lambda: _display(device.token),
        ),
        DifferentialProbe(
            "apps.scheduling.reception_views.waiting_display",
            lambda: _display("invalid"),
            expected=False,
        ),
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
