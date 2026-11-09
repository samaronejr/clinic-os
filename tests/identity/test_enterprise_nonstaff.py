"""A tenant GUC is never sufficient for directory, pre-auth or support access."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.identity.scim import create_resource, scim_context
from apps.intake.models import Patient, PatientClinicEnrollment
from django.db import connection
from django.test import override_settings

from identity.enterprise_support import enterprise_setup
from identity.permission_support import permission_actor, permission_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_scim_token_cannot_read_staff_patient_tables(rbac_graph: RbacGraph) -> None:
    # Given real clinical rows and a separate directory-only bearer.
    permission_actor(rbac_graph, "physician")
    setup = enterprise_setup(rbac_graph)
    with runtime_role(), scim_context(bearer=setup.token) as scope:
        # When the same connection attempts ordinary tenant ORM reads.
        patients = tuple(Patient.objects.all())
        enrollments = tuple(PatientClinicEnrollment.objects.all())
        # Then even generic tenant policies refuse clinical rows while SCIM works.
        assert patients == ()
        assert enrollments == ()
        resource = create_resource(
            clinic_id=scope.clinic_id,
            kind="Users",
            attributes={
                "userName": "synthetic-directory",
                "externalId": "synthetic-sub",
            },
        )
        assert resource.clinic_id == scope.clinic_id


@pytest.mark.parametrize(
    "guc", ["app.current_sso_connection", "app.current_support_grant"]
)
def test_nonstaff_context_cannot_borrow_an_existing_staff_actor(
    rbac_graph: RbacGraph, guc: str
) -> None:
    # Given actual clinical data and a legitimate staff GUC deliberately mixed
    # with a non-staff context, representing an accidental future code path.
    clinician, _enrollment = permission_actor(rbac_graph, "physician")
    with permission_context(rbac_graph, clinician), connection.cursor() as cursor:
        cursor.execute("SELECT set_config(%s,%s,true)", [guc, str(clinician)])
        # When a direct ORM reader bypasses the Python service guard.
        result = tuple(Patient.objects.all())
        # Then PostgreSQL itself refuses, not merely the HTTP view.
        assert result == ()
