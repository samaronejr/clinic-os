from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.identity.models import Clinic, Organization, User, UserClinicRole
from apps.tenancy.models import TenantProbe
from django.contrib.auth.hashers import make_password
from django.db import connection, transaction

from database_urls import database_url_for_name

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

SYNTHETIC_AUTH_VALUE_A = "synthetic-hash-a"
SYNTHETIC_AUTH_VALUE_B = "synthetic-hash-b"
AUDIT_ROW_TRIGGER = "audit_event_immutable_row"
AUDIT_TRUNCATE_TRIGGER = "audit_event_immutable_truncate"
RBAC_RAW_CREDENTIAL = "todo8-correct-horse-battery-staple"


@dataclass(frozen=True, slots=True)
class TenantGraph:
    organization_a: UUID
    organization_b: UUID
    user_a: UUID
    user_b: UUID
    username_a: str


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


@pytest.fixture
def app_database_url() -> str:
    database_name = str(connection.settings_dict["NAME"])
    return database_url_for_name(os.environ["APP_DATABASE_URL"], database_name)


@pytest.fixture
def superuser_database_url() -> str:
    database_name = str(connection.settings_dict["NAME"])
    return database_url_for_name(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], database_name
    )


def _test_superuser_database_url() -> str:
    database_name = str(connection.settings_dict["NAME"])
    return database_url_for_name(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], database_name
    )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(
    item: pytest.Item,
    nextitem: pytest.Item | None,
) -> Generator[None, None, None]:
    marker = item.get_closest_marker("django_db")
    transactional = marker is not None and bool(marker.kwargs.get("transaction", False))
    if not transactional:
        yield
        return
    database_url = _test_superuser_database_url()
    triggers_present = False
    started_enabled = True
    with psycopg.connect(database_url) as raw_connection:
        rows = raw_connection.execute(
            "SELECT tgname, tgenabled FROM pg_trigger "
            "WHERE tgrelid = to_regclass('clinic_app.audit_event') "
            "AND tgname IN (%s, %s) ORDER BY tgname",
            [AUDIT_ROW_TRIGGER, AUDIT_TRUNCATE_TRIGGER],
        ).fetchall()
        if len(rows) == 2:
            triggers_present = True
            started_enabled = all(row[1] == "O" for row in rows)
            raw_connection.execute(
                f"ALTER TABLE clinic_app.audit_event ENABLE TRIGGER {AUDIT_ROW_TRIGGER}"
            )
            raw_connection.execute(
                "ALTER TABLE clinic_app.audit_event ENABLE TRIGGER "
                f"{AUDIT_TRUNCATE_TRIGGER}"
            )
            raw_connection.execute(
                "ALTER TABLE clinic_app.audit_event DISABLE TRIGGER "
                f"{AUDIT_ROW_TRIGGER}"
            )
            raw_connection.execute(
                "ALTER TABLE clinic_app.audit_event DISABLE TRIGGER "
                f"{AUDIT_TRUNCATE_TRIGGER}"
            )
    try:
        yield
    finally:
        if triggers_present:
            with psycopg.connect(database_url) as raw_connection:
                raw_connection.execute(
                    "ALTER TABLE clinic_app.audit_event ENABLE TRIGGER "
                    f"{AUDIT_ROW_TRIGGER}"
                )
                raw_connection.execute(
                    "ALTER TABLE clinic_app.audit_event ENABLE TRIGGER "
                    f"{AUDIT_TRUNCATE_TRIGGER}"
                )
                rows = raw_connection.execute(
                    "SELECT tgname, tgenabled FROM pg_trigger "
                    "WHERE tgrelid = 'clinic_app.audit_event'::regclass "
                    "AND tgname IN (%s, %s) ORDER BY tgname",
                    [AUDIT_ROW_TRIGGER, AUDIT_TRUNCATE_TRIGGER],
                ).fetchall()
            assert rows == [
                (AUDIT_ROW_TRIGGER, "O"),
                (AUDIT_TRUNCATE_TRIGGER, "O"),
            ]
        assert started_enabled is True


@pytest.fixture(scope="session")
def audit_triggers_finally_enabled(
    django_db_setup: None,
) -> Iterator[None]:
    yield
    with psycopg.connect(_test_superuser_database_url()) as raw_connection:
        raw_connection.execute(
            f"ALTER TABLE clinic_app.audit_event ENABLE TRIGGER {AUDIT_ROW_TRIGGER}"
        )
        raw_connection.execute(
            "ALTER TABLE clinic_app.audit_event ENABLE TRIGGER "
            f"{AUDIT_TRUNCATE_TRIGGER}"
        )
        rows = raw_connection.execute(
            "SELECT tgname, tgenabled FROM pg_trigger "
            "WHERE tgrelid = 'clinic_app.audit_event'::regclass "
            "AND tgname IN (%s, %s) ORDER BY tgname",
            [AUDIT_ROW_TRIGGER, AUDIT_TRUNCATE_TRIGGER],
        ).fetchall()
    assert rows == [
        (AUDIT_ROW_TRIGGER, "O"),
        (AUDIT_TRUNCATE_TRIGGER, "O"),
    ]
