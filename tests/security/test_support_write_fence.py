"""SA-T1: an approved support grant never carries clinical write authority."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from apps.ehr.models import ClinicalDocumentVersion
from apps.ehr.services import ClinicalAccessDeniedError
from apps.identity.enterprise_access import approve_support_access
from apps.tenancy.db import tenant_context
from django.db import connection, transaction
from django.test import override_settings
from django.utils import timezone

from identity.enterprise_support import enterprise_setup
from identity.permission_support import permission_context
from patient_service_support import runtime_role
from renewal.test_amendments import saved_draft
from renewal.test_encounters import seed

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_clinical_write_is_refused_while_a_support_grant_is_in_context(
    rbac_graph: RbacGraph,
) -> None:
    # Given a customer-approved support grant naming the assigned physician, the
    # identity that support_diagnostics installs for its audit append.
    setup = enterprise_setup(rbac_graph)
    with permission_context(rbac_graph, rbac_graph.clinic_admin):
        grant = approve_support_access(
            clinic_id=rbac_graph.clinic_a,
            support_user_id=rbac_graph.physician,
            purpose="Synthetic diagnostic",
            expires_at=timezone.now() + timedelta(minutes=30),
            session_key=setup.session_key,
        )
    appointment, template = seed(rbac_graph)
    # When that physician writes a clinical note while the grant is in context,
    # the fenced policies hide the clinical scope, so the write is refused and
    # nothing is stored ...
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_support_grant',%s,true)",
                [str(grant.pk)],
            )
        with pytest.raises(ClinicalAccessDeniedError), transaction.atomic():
            saved_draft(rbac_graph, appointment, template)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        assert not ClinicalDocumentVersion.objects.exists()
        # ... while the same physician's write without the grant succeeds.
        saved_draft(rbac_graph, appointment, template)
        assert ClinicalDocumentVersion.objects.count() == 1
