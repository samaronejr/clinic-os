"""Synthetic source worlds with real professional and tenant authorization."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from apps.core.secrets import secret_store
from apps.identity.models import ServicePrincipal
from apps.intake.models import Patient
from apps.orders.lab import receive_lab_result
from apps.orders.models import LabAccount, Order
from apps.orders.services import (
    IdentityEvidence,
    IngestReceipt,
    ResultInput,
    place_order,
)

from ehr.longitudinal_support import chart_world
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

SOURCE = b"%PDF-1.4\nsynthetic source result\n%%EOF"


@dataclass(frozen=True, slots=True)
class World:
    order: Order
    identity: IdentityEvidence
    enrollment: UUID


def ordered(graph: RbacGraph, *, lab: UUID | None = None) -> World:
    chart = chart_world(graph)
    with owner_context(graph.organization_a):
        patient = Patient.objects.get(pk=chart.patient)
        identity = IdentityEvidence(
            birth_date=patient.birth_date, name=patient.full_name
        )
    with as_actor(graph, graph.physician):
        order = place_order(
            clinic_id=graph.clinic_a,
            enrollment_id=chart.enrollment,
            exam_codes=("cbc",),
            command_id=uuid4(),
            lab_account_id=lab,
        )
    return World(order, identity, chart.enrollment)


def submission(world: World) -> ResultInput:
    return ResultInput(
        order_id=world.order.pk,
        identity=world.identity,
        content_type="application/pdf",
        data=SOURCE,
        event_id=uuid4(),
    )


@dataclass(frozen=True, slots=True)
class LabWorld:
    world: World
    account: UUID
    key: str = field(repr=False)
    principal: UUID

    def body(self, *, event: UUID | None = None) -> bytes:
        return json.dumps(
            {
                "correlation": str(self.world.order.correlation),
                "event_id": str(event or uuid4()),
                "identity": self.world.identity.payload(),
                "content_type": "application/pdf",
                "data_base64": base64.b64encode(SOURCE).decode("ascii"),
                "observations": [],
                "critical": False,
                "result_id": None,
                "predecessor_id": None,
            }
        ).encode()

    def headers(self, body: bytes) -> dict[str, str]:
        return {
            "X-Lab-Account": str(self.account),
            "X-Lab-Signature": hmac.digest(
                bytes.fromhex(self.key), body, hashlib.sha256
            ).hex(),
        }

    def receive(self, body: bytes) -> IngestReceipt:
        with runtime_role():
            return receive_lab_result(headers=self.headers(body), body=body)


def laboratory(graph: RbacGraph, settings: SettingsWrapper) -> LabWorld:
    settings.ORDERS_SYNTHETIC_LAB_ENABLED = True
    key = secrets.token_hex(32)
    with owner_context(graph.organization_a):
        principal = ServicePrincipal.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            name=f"lab-{uuid4().hex}",
            db_identity=f"clinic_agent_lab_{uuid4().hex}",
            purpose="lab_ingest",
        )
        account = LabAccount.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            principal=principal,
            signing_key_digest=hashlib.sha256(key.encode()).hexdigest(),
        )
    backend = secret_store()
    # The test backend's directory is private and already owned by its fixture.
    root = Path(str(settings.CLINIC_SECRET_DIR))
    if not root.is_dir():
        raise AssertionError(backend)
    path = root / f"orders-lab-{account.pk.hex}.secret"
    path.write_text(key)
    path.chmod(0o600)
    return LabWorld(ordered(graph, lab=account.pk), account.pk, key, principal.pk)
