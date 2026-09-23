from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

import psycopg
from apps.identity.models import Clinic, Organization, User
from django.db import connection
from psycopg.errors import DeadlockDetected, ExclusionViolation

if TYPE_CHECKING:
    from queue import Queue
    from threading import Barrier

ORG_ID = UUID(int=5001)
CLINIC_A_ID = UUID(int=5101)
CLINIC_B_ID = UUID(int=5102)
PRACTITIONER_A_ID = UUID(int=5201)
PRACTITIONER_B_ID = UUID(int=5202)
START = datetime(2030, 1, 2, 14, tzinfo=UTC)
END = START + timedelta(hours=1)


def set_tenant(organization_id: UUID) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, false)",
            [str(organization_id)],
        )


def seed() -> tuple[Organization, Clinic, Clinic, User, User]:
    set_tenant(ORG_ID)
    organization = Organization.objects.create(
        id=ORG_ID,
        name="Synthetic Scheduling Organization",
        cnpj="00000000005001",
    )
    clinic_a = Clinic.objects.create(
        id=CLINIC_A_ID,
        organization=organization,
        name="Synthetic Scheduling Clinic A",
        crm_uf="SP",
        timezone="America/Sao_Paulo",
    )
    clinic_b = Clinic.objects.create(
        id=CLINIC_B_ID,
        organization=organization,
        name="Synthetic Scheduling Clinic B",
        crm_uf="SP",
        timezone="America/Sao_Paulo",
    )
    practitioner_a = User.objects.create(
        id=PRACTITIONER_A_ID,
        username="synthetic-practitioner-a",
    )
    practitioner_b = User.objects.create(
        id=PRACTITIONER_B_ID,
        username="synthetic-practitioner-b",
    )
    return organization, clinic_a, clinic_b, practitioner_a, practitioner_b


def race_insert(
    database_url: str,
    barrier: Barrier,
    call: tuple[UUID, UUID, UUID],
    results: Queue[str],
) -> None:
    row_id, clinic_id, key = call
    with psycopg.connect(database_url) as raw_connection:
        raw_connection.execute("SET LOCAL statement_timeout = '5s'")
        raw_connection.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            (str(ORG_ID),),
        )
        barrier.wait(timeout=3)
        try:
            raw_connection.execute(
                "INSERT INTO clinic_app.scheduling_availabilityblock "
                "(id, organization_id, clinic_id, practitioner_id, start_at, end_at, "
                "idempotency_key, create_fingerprint, created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (
                    row_id,
                    ORG_ID,
                    clinic_id,
                    PRACTITIONER_A_ID,
                    START,
                    END,
                    key,
                    b"r" * 32,
                ),
            )
        except (DeadlockDetected, ExclusionViolation):
            raw_connection.rollback()
            results.put("conflict")
        else:
            raw_connection.commit()
            results.put("inserted")
