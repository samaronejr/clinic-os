"""Outermost transaction boundaries must be invoked outside tenant_context."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.tenancy.db import TenantAccessDeniedError, tenant_context
from apps.tenancy.middleware import TenantMiddleware
from django.contrib.auth import SESSION_KEY
from django.db import connection
from django.http import HttpResponse

from patient_service_support import runtime_role

if TYPE_CHECKING:
    from identity.legacy_parity_support import LegacyWorld

TARGETS = (
    "apps.tenancy.db.tenant_context",
    "apps.tenancy.middleware.TenantMiddleware.__call__",
)


def exercise_tenant_boundaries(w: LegacyWorld) -> None:
    for valid in (True, False):
        organization = w.graph.organization_a if valid else w.graph.organization_b
        with runtime_role():
            try:
                with (
                    tenant_context(w.actor.pk, organization),
                    connection.cursor() as cursor,
                ):
                    cursor.execute("SELECT current_setting('app.current_tenant')")
                    assert cursor.fetchone() == (str(organization),)
            except TenantAccessDeniedError:
                allowed = False
            else:
                allowed = True
            assert allowed is valid
        w.request.path_info = "/workspace/"
        w.request.session[SESSION_KEY] = str(w.actor.pk)
        w.request.session["active_org_id"] = str(organization)
        with runtime_role():
            response = TenantMiddleware(lambda _request: HttpResponse(status=204))(
                w.request
            )
        assert response.status_code == (204 if valid else 403)
