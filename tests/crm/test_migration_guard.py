"""A tenantless migration owner must not mistake FORCE RLS for an empty schema."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.crm.migrations._crm_sql import REVERSE
from apps.crm.models import Lead
from apps.crm.services import create_lead
from apps.identity.models import UserClinicRole
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction

from auth.stepup_test_support import create_role_actor
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_populated_reverse_refuses_without_a_tenant_guc(rbac_graph: RbacGraph) -> None:
    actor = create_role_actor(rbac_graph, UserClinicRole.Role.CLINIC_MANAGER)
    with runtime_role(), tenant_context(actor.pk, rbac_graph.organization_a):
        lead = create_lead(
            clinic_id=rbac_graph.clinic_a,
            name="Synthetic retained prospect",
            email="retained@example.invalid",
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_user, NULLIF(current_setting('app.current_tenant',true),'')"
        )
        assert cursor.fetchone() == ("clinic_owner", None)
    # The outer atomic also restores every trigger/function if a broken guard
    # reaches the following DROP statements and pytest raises its failure.
    with (
        transaction.atomic(),
        connection.cursor() as cursor,
        pytest.raises(DatabaseError, match="populated CRM rollback requires restore"),
    ):
        cursor.execute(REVERSE)
    with runtime_role(), tenant_context(actor.pk, rbac_graph.organization_a):
        assert Lead.objects.get(pk=lead.pk).state == "active"
