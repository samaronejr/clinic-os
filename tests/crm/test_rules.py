"""Default-deny outreach content and manager authority."""

from uuid import uuid4

import pytest
from apps.crm.access import CrmAccessDeniedError
from apps.crm.campaigns import create_campaign
from apps.crm.rules import MARKETING_BODIES
from apps.crm.services import create_lead
from apps.identity.models import UserClinicRole
from django.db import DatabaseError, connection, transaction

from auth.stepup_test_support import create_role_actor
from patient_service_support import runtime_role
from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_no_context_cannot_create_lead() -> None:
    with runtime_role(), pytest.raises(CrmAccessDeniedError):
        create_lead(
            clinic_id=uuid4(),
            name="Synthetic prospect",
            email="synthetic@example.invalid",
        )


def test_reception_cannot_grant_own_marketing_campaign(rbac_graph: RbacGraph) -> None:
    from apps.tenancy.db import tenant_context  # noqa: PLC0415

    receptionist = create_role_actor(rbac_graph, UserClinicRole.Role.RECEPTIONIST)
    with (
        runtime_role(),
        tenant_context(receptionist.pk, rbac_graph.organization_a),
        pytest.raises(CrmAccessDeniedError),
    ):
        create_campaign(
            clinic_id=rbac_graph.clinic_a,
            title="Synthetic campaign",
            template_version_id=uuid4(),
        )


def test_neutral_copy_has_no_diagnosis_variables() -> None:
    assert MARKETING_BODIES
    for body in MARKETING_BODIES:
        assert "{" not in body
        assert "}" not in body
        assert "diagnos" not in body.lower()


def test_new_campaign_table_is_force_rls() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity,relforcerowsecurity "
            "FROM pg_class WHERE oid='clinic_app.crm_campaign'::regclass"
        )
        assert cursor.fetchone() == (True, True)


def test_raw_campaign_delete_is_denied() -> None:
    with (
        runtime_role(),
        transaction.atomic(),
        connection.cursor() as cursor,
        pytest.raises(DatabaseError),
    ):
        cursor.execute("DELETE FROM clinic_app.crm_campaign")
