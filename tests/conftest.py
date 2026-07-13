from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.identity.models import Clinic, Organization, User, UserClinicRole
from apps.tenancy.models import TenantProbe
from django.db import connection, transaction

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

SYNTHETIC_AUTH_VALUE_A = "synthetic-hash-a"
SYNTHETIC_AUTH_VALUE_B = "synthetic-hash-b"
AUDIT_ROW_TRIGGER = "audit_event_immutable_row"
AUDIT_TRUNCATE_TRIGGER = "audit_event_immutable_truncate"


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


def _test_superuser_database_url() -> str:
    configured_url = urlsplit(os.environ["TEST_SUPERUSER_DATABASE_URL"])
    database_name = str(connection.settings_dict["NAME"])
    return urlunsplit(configured_url._replace(path=f"/{database_name}"))


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
