"""Synthetic real-role CRM world; no provider calls or persisted test secrets."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from apps.comms.inbox_adapters import (
    EmailInboxAdapter,
    message_lock_key,
    message_send_eligible,
)
from apps.comms.inbox_services import create_template_version, set_template_approval
from apps.comms.models import ProviderAccount
from apps.consent.services import prepare_acceptance, publish_text, record_consent
from apps.core.integration import register_send_adapter, register_subject_recheck
from apps.crm.rules import MARKETING_BODIES
from apps.identity.models import ServicePrincipal, UserClinicRole
from apps.intake.contacts import save_contact_destination, verify_contact
from apps.intake.patient_access import (
    issue_invitation,
    patient_session_context,
    redeem_invitation,
)
from apps.intake.services import create_patient
from apps.tenancy.db import tenant_context

from auth.stepup_test_support import create_role_actor
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from renewal.test_retention import admin

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph


@dataclass(frozen=True)
class CrmWorld:
    """One manager, neutral template and independently verified patient."""

    graph: RbacGraph
    manager: UUID
    enrollment: UUID
    patient: UUID
    session: UUID
    text: UUID
    template: UUID


def world(graph: RbacGraph, settings: SettingsWrapper) -> CrmWorld:
    """Seed using domain services under exact runtime actors."""
    settings.COMMS_SYNTHETIC_CHANNELS = ("email",)
    actor = create_role_actor(graph, UserClinicRole.Role.CLINIC_MANAGER)
    publisher = admin(graph)
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        registered = create_patient(
            clinic_id=graph.clinic_a,
            full_name="Synthetic CRM Patient",
            birth_date=date(1990, 1, 1),
            idempotency_key=uuid4(),
        )
        contact = save_contact_destination(
            clinic_id=graph.clinic_a,
            enrollment_id=registered.enrollment.pk,
            channel="email",
            destination=f"synthetic-{uuid4().hex}@example.invalid",
            expected_version=None,
        )
        verify_contact(
            clinic_id=graph.clinic_a,
            enrollment_id=registered.enrollment.pk,
            channel="email",
            expected_version=contact.destination_version,
        )
        invitation = issue_invitation(
            clinic_id=graph.clinic_a, enrollment_id=registered.enrollment.pk
        )
    with runtime_role():
        session_id = redeem_invitation(graph.clinic_a, invitation.secret)
    assert session_id is not None
    with runtime_role(), tenant_context(publisher.pk, graph.organization_a):
        text = publish_text(
            clinic_id=graph.clinic_a,
            purpose="marketing",
            text="Synthetic optional marketing consent. Refusal does not affect care.",
        )
    with runtime_role(), tenant_context(actor.pk, graph.organization_a):
        template = create_template_version(
            clinic_id=graph.clinic_a,
            channel="email",
            name=f"neutral_{uuid4().hex}",
            language="pt_BR",
            category="marketing",
            purpose="marketing",
            body=MARKETING_BODIES[0],
        )
        for status in ("pending", "approved"):
            set_template_approval(
                clinic_id=graph.clinic_a, template_version_id=template, status=status
            )
    with owner_context(graph.organization_a):
        principal = ServicePrincipal.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            name=f"crm-{uuid4().hex}",
            db_identity=f"clinic_agent_crm_{uuid4().hex}",
            purpose="messaging_ingest",
        )
        ProviderAccount.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            channel="email",
            provider="inbox-email-v1",
            external_account_id=f"synthetic-{uuid4().hex}",
            principal=principal,
            signing_key_digest=hashlib.sha256(
                b"synthetic-not-a-credential"
            ).hexdigest(),
        )
    register_send_adapter(EmailInboxAdapter())
    register_subject_recheck(
        "comms.message", message_send_eligible, lock_key=message_lock_key
    )
    return CrmWorld(
        graph,
        actor.pk,
        registered.enrollment.pk,
        registered.patient.pk,
        session_id,
        text.pk,
        template,
    )


def accept_marketing(state: CrmWorld) -> UUID:
    """Accept the exact optional text as the patient, independent of channel."""
    with runtime_role(), patient_session_context(state.session):
        _, offer = prepare_acceptance(text_id=state.text)
        return record_consent(offer=offer, purpose="marketing", accepted=True).pk
