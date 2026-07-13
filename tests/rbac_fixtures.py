from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from apps.identity.models import Clinic, Organization, User, UserClinicRole
from django.contrib.auth.hashers import make_password
from django.db import connection, transaction

RBAC_RAW_CREDENTIAL = "todo8-correct-horse-battery-staple"


@dataclass(frozen=True, slots=True)
class RbacGraph:
    organization_a: UUID
    organization_b: UUID
    clinic_a: UUID
    clinic_b: UUID
    clinic_c: UUID
    physician: UUID
    clinic_admin: UUID
    shared_user: UUID
    shared_username: str
    no_membership_username: str


@pytest.fixture
def rbac_graph() -> RbacGraph:
    organization_a = uuid4()
    organization_b = uuid4()
    physician = User.objects.create(
        username=f"todo8-physician-{uuid4().hex}",
        password=make_password(RBAC_RAW_CREDENTIAL),
    )
    clinic_admin = User.objects.create(
        username=f"todo8-admin-{uuid4().hex}",
        password=make_password(RBAC_RAW_CREDENTIAL),
    )
    shared_username = f"todo8-shared-{uuid4().hex}"
    shared_user = User.objects.create(
        username=shared_username,
        password=make_password(RBAC_RAW_CREDENTIAL),
    )
    no_membership_username = f"todo8-no-membership-{uuid4().hex}"
    User.objects.create(
        username=no_membership_username,
        password=make_password(RBAC_RAW_CREDENTIAL),
    )

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_a)],
        )
        org_a = Organization.objects.create(
            id=organization_a,
            name="Todo 8 Organization A",
            cnpj=f"{int(organization_a) % 10**14:014d}",
        )
        clinic_a = Clinic.objects.create(
            organization=org_a,
            name="Todo 8 Clinic A",
            crm_uf="SP",
        )
        clinic_b = Clinic.objects.create(
            organization=org_a,
            name="Todo 8 Clinic B",
            crm_uf="RJ",
        )
        UserClinicRole.objects.create(
            user_id=physician.pk,
            organization_id=organization_a,
            clinic_id=clinic_a.pk,
            role=UserClinicRole.Role.PHYSICIAN,
        )
        UserClinicRole.objects.create(
            user_id=clinic_admin.pk,
            organization_id=organization_a,
            clinic_id=clinic_b.pk,
            role=UserClinicRole.Role.CLINIC_ADMIN,
        )
        UserClinicRole.objects.create(
            user_id=shared_user.pk,
            organization_id=organization_a,
            clinic_id=clinic_a.pk,
            role=UserClinicRole.Role.RECEPTIONIST,
        )

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_b)],
        )
        org_b = Organization.objects.create(
            id=organization_b,
            name="Todo 8 Organization B",
            cnpj=f"{int(organization_b) % 10**14:014d}",
        )
        clinic_c = Clinic.objects.create(
            organization=org_b,
            name="Todo 8 Clinic C",
            crm_uf="MG",
        )
        UserClinicRole.objects.create(
            user_id=shared_user.pk,
            organization_id=organization_b,
            clinic_id=clinic_c.pk,
            role=UserClinicRole.Role.PHYSICIAN,
        )

    return RbacGraph(
        organization_a=organization_a,
        organization_b=organization_b,
        clinic_a=clinic_a.pk,
        clinic_b=clinic_b.pk,
        clinic_c=clinic_c.pk,
        physician=physician.pk,
        clinic_admin=clinic_admin.pk,
        shared_user=shared_user.pk,
        shared_username=shared_username,
        no_membership_username=no_membership_username,
    )
