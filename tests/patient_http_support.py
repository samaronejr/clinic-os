from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from apps.identity.models import User
from apps.intake.services import create_patient
from apps.tenancy.db import tenant_context
from django.db import connection
from django.test import Client

from otp_test_support import (
    OTP_RAW_CREDENTIAL,
    create_receptionist,
    create_totp_device,
    fixed_otp_time,
    get_totp_device,
    login,
    runtime_role,
    token_for,
)
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

SEARCH_EVENT: Final = "intake.patient.searched"
SYNTHETIC_SURNAME: Final = "Synthetic Testpatient"


def patient_list_url(clinic_id: UUID) -> str:
    return f"/intake/clinics/{clinic_id}/patients/"


def patient_create_url(clinic_id: UUID) -> str:
    return f"/intake/clinics/{clinic_id}/patients/new/"


def receptionist_client(graph: RbacGraph) -> tuple[Client, User]:
    receptionist = create_receptionist(graph)
    client = Client()
    with runtime_role():
        assert client.login(
            username=receptionist.username,
            password=OTP_RAW_CREDENTIAL,
        )
    return client, receptionist


def verified_physician_client(graph: RbacGraph) -> Client:
    create_totp_device(graph.physician, confirmed=True)
    username = User.objects.get(pk=graph.physician).username
    client = Client()
    with runtime_role(), fixed_otp_time():
        login(client, username, password=RBAC_RAW_CREDENTIAL)
        device = get_totp_device(graph.physician, confirmed=True)
        client.post(
            "/auth/verify/",
            {
                "otp_device": device.persistent_id,
                "otp_token": token_for(device),
                "next": "/auth/protected/",
            },
        )
    return client


def seed_patients(
    graph: RbacGraph,
    actor: UUID,
    clinic_id: UUID,
    names: tuple[str, ...],
) -> None:
    with runtime_role(), tenant_context(actor, graph.organization_a):
        for index, name in enumerate(names):
            create_patient(
                clinic_id=clinic_id,
                full_name=name,
                birth_date=date(1990, 1, 1 + index % 27),
                idempotency_key=uuid4(),
            )


def audit_event_types(graph: RbacGraph, actor: UUID) -> list[str]:
    with (
        runtime_role(),
        tenant_context(actor, graph.organization_a),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT event_type FROM clinic_app.audit_event_tenant ORDER BY seq"
        )
        return [str(row[0]) for row in cursor.fetchall()]


def audit_payloads(
    graph: RbacGraph,
    actor: UUID,
    event_type: str,
) -> list[dict[str, str]]:
    with (
        runtime_role(),
        tenant_context(actor, graph.organization_a),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT key, value FROM clinic_app.audit_event_tenant, "
            "LATERAL jsonb_each_text(payload) "
            "WHERE event_type = %s ORDER BY seq, key",
            [event_type],
        )
        return [{"key": str(row[0]), "value": str(row[1])} for row in cursor.fetchall()]
