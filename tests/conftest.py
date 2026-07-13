from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import pytest
from apps.identity.models import Clinic, Organization, User, UserClinicRole
from apps.tenancy.models import TenantProbe
from django.db import connection, transaction

SYNTHETIC_AUTH_VALUE_A = "synthetic-hash-a"
SYNTHETIC_AUTH_VALUE_B = "synthetic-hash-b"


@dataclass(frozen=True, slots=True)
class TenantGraph:
    organization_a: UUID
    organization_b: UUID
    user_a: UUID
    user_b: UUID
    username_a: str


def _create_tenant_rows(
    organization_id: UUID,
    user: User,
    *,
    label: str,
) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        organization = Organization.objects.create(
            id=organization_id,
            name=f"Synthetic Organization {label}",
            cnpj=f"{int(organization_id) % 10**14:014d}",
        )
        clinic = Clinic.objects.create(
            organization=organization,
            name=f"Synthetic Clinic {label}",
            crm_uf="SP",
        )
        UserClinicRole.objects.create(
            user=user,
            organization=organization,
            clinic=clinic,
            role=UserClinicRole.Role.OWNER,
        )
        TenantProbe.objects.create(
            organization=organization,
            label=f"probe-{label.lower()}",
        )


@pytest.fixture
def tenant_graph() -> TenantGraph:
    organization_a = uuid4()
    organization_b = uuid4()
    username_a = f"synthetic-user-a-{uuid4().hex}"
    user_a = User.objects.create(username=username_a, password=SYNTHETIC_AUTH_VALUE_A)
    user_b = User.objects.create(
        username=f"synthetic-user-b-{uuid4().hex}",
        password=SYNTHETIC_AUTH_VALUE_B,
    )
    _create_tenant_rows(organization_a, user_a, label="A")
    _create_tenant_rows(organization_b, user_b, label="B")
    return TenantGraph(
        organization_a=organization_a,
        organization_b=organization_b,
        user_a=user_a.pk,
        user_b=user_b.pk,
        username_a=username_a,
    )


@pytest.fixture
def app_database_url() -> str:
    configured_url = urlsplit(os.environ["APP_DATABASE_URL"])
    database_name = str(connection.settings_dict["NAME"])
    return urlunsplit(configured_url._replace(path=f"/{database_name}"))


@pytest.fixture
def superuser_database_url() -> str:
    configured_url = urlsplit(os.environ["TEST_SUPERUSER_DATABASE_URL"])
    database_name = str(connection.settings_dict["NAME"])
    return urlunsplit(configured_url._replace(path=f"/{database_name}"))
