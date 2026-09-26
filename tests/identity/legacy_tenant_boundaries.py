"""Outermost transaction boundaries must be invoked outside tenant_context."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.identity.models import UserClinicRole
from apps.tenancy.db import TenantAccessDeniedError, tenant_context
from apps.tenancy.envelope import EnvelopeError, blind_indexes
from apps.tenancy.middleware import TenantMiddleware
from django.contrib.auth import SESSION_KEY
from django.db import connection, transaction
from django.http import HttpResponse

from auth.stepup_test_support import create_role_actor
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from identity.legacy_parity_support import LegacyWorld

TARGETS = (
    "apps.tenancy.db.tenant_context",
    "apps.tenancy.middleware.TenantMiddleware.__call__",
    "apps.tenancy.envelope.blind_indexes",
)
_BLIND_PURPOSE = "intake.patientidentifier.value"
_BLIND_PLAINTEXT = b"cpf:52998224725"


def _blind_digests() -> tuple[bytes, ...]:
    return tuple(
        index.digest
        for index in blind_indexes(purpose=_BLIND_PURPOSE, plaintext=_BLIND_PLAINTEXT)
    )


def exercise_blind_index(w: LegacyWorld) -> None:
    """Differential oracle for the tenant-keyed blind index primitive.

    ``blind_indexes`` makes no staff decision: callers gate it. Its contract
    is tenant binding and fail-closed context, so the oracle executes it
    and compares: every legacy role and an organization member holding no
    demographics permission get the identical digest in one tenant; another
    tenant's DEK yields a different digest; no tenant context yields no
    digest at all.
    """
    with runtime_role(), tenant_context(w.actor.pk, w.graph.organization_a):
        mine = _blind_digests()
    assert mine
    permissionless = create_role_actor(w.graph, UserClinicRole.Role.ORG_ADMIN)
    with runtime_role(), tenant_context(permissionless.pk, w.graph.organization_a):
        assert _blind_digests() == mine
    with runtime_role(), tenant_context(w.graph.shared_user, w.graph.organization_b):
        assert set(_blind_digests()).isdisjoint(mine)
    with runtime_role(), pytest.raises(EnvelopeError), transaction.atomic():
        _blind_digests()


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
    exercise_blind_index(w)
