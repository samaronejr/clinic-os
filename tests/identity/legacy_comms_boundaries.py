"""Staff inbox role gates, executed on the real callables for every stored role.

Who may handle an inbox thread follows the actor's stored clinic roles and the
thread's purpose. Each boundary below runs as the signed-in staff actor, in the
clinic where the role is held (valid) and in a clinic where it holds none
(invalid). The expected role sets are written out here, independently of the
constants the services read, so a widened or narrowed set fails the comparison.
"""

from __future__ import annotations

from datetime import date, timedelta
from hashlib import sha256
from secrets import token_hex
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.comms import inbox_queries
from apps.comms import inbox_services as services
from apps.comms.inbox_feed import question_items
from apps.comms.inbox_queries import care_team_patients
from apps.comms.models import Conversation, ProviderAccount
from apps.identity.models import CareTeamMembership, ServicePrincipal
from apps.intake.models import Patient, PatientClinicEnrollment
from apps.workflows.inbox import InboxScope
from django.utils import timezone

from identity.legacy_parity_support import Boundary
from identity.permission_support import owner_context

if TYPE_CHECKING:
    from identity.legacy_parity_support import LegacyWorld

MANAGERS = ("clinic_admin", "clinic_manager")
CLINICIANS = ("nurse", "physician")
CARE_TEAM_ROLES = ("nurse", "physician")
INBOX_STAFF = (
    "clinic_admin",
    "clinic_manager",
    "receptionist",
    "scheduler",
    "nurse",
    "physician",
)


def seed_comms(subject: LegacyWorld) -> None:
    """One clinic-A thread that is waiting for a clinician, set up as the owner."""
    graph = subject.graph
    now = timezone.now()
    with owner_context(graph.organization_a):
        principal = ServicePrincipal.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            name=f"inbox-{uuid4().hex}",
            db_identity=f"clinic_agent_msg_{uuid4().hex}",
            purpose="messaging_ingest",
        )
        account = ProviderAccount.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            channel="whatsapp",
            provider="whatsapp-cloud-v1",
            external_account_id=uuid4().hex,
            principal=principal,
            signing_key_digest=sha256(token_hex(32).encode()).hexdigest(),
        )
        Conversation.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            provider_account=account,
            channel="whatsapp",
            purpose="transactional",
            contact_fingerprint=uuid4().hex + uuid4().hex,
            contact_address="+5511999990777",
            last_inbound_at=now,
            last_activity_at=now,
            needs_clinician=True,
            triage_category="clinical",
        )
        if subject.role in CARE_TEAM_ROLES:
            patient = Patient.objects.create(
                organization_id=graph.organization_a,
                full_name="Sintetico care team",
                birth_date=date(1990, 1, 1),
            )
            enrollment = PatientClinicEnrollment.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                patient=patient,
                idempotency_key=uuid4(),
                create_fingerprint=b"e" * 32,
            )
            CareTeamMembership.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                patient_enrollment=enrollment,
                user_id=subject.actor.pk,
                role=subject.role,
                valid_from=now - timedelta(days=1),
            )


def _care_team(subject: LegacyWorld, valid: bool) -> bool:
    return bool(care_team_patients(subject.clinic_for(valid)))


def _waiting_questions(subject: LegacyWorld, valid: bool) -> bool:
    scope = InboxScope(
        clinic_id=subject.clinic_for(valid),
        actor_id=subject.actor.pk,
        timezone="UTC",
        now=timezone.now(),
    )
    return bool(question_items(scope))


BOUNDARIES = (
    Boundary(
        "apps.comms.inbox_services._actor",
        "comms",
        MANAGERS,
        lambda w, valid: services._actor(w.clinic_for(valid), services.MANAGER_ROLES),
    ),
    Boundary(
        "apps.comms.inbox_services._actor",
        "comms",
        CLINICIANS,
        lambda w, valid: services._actor(w.clinic_for(valid), services.CLINICIAN_ROLES),
    ),
    Boundary(
        "apps.comms.inbox_queries.visible_purposes",
        "comms",
        MANAGERS,
        lambda w, valid: (
            "marketing" in inbox_queries.visible_purposes(w.clinic_for(valid))
        ),
    ),
    Boundary(
        "apps.comms.inbox_queries.visible_purposes",
        "comms",
        CLINICIANS,
        lambda w, valid: "care" in inbox_queries.visible_purposes(w.clinic_for(valid)),
    ),
    Boundary(
        "apps.comms.inbox_queries.visible_purposes",
        "comms",
        INBOX_STAFF,
        lambda w, valid: (
            "transactional" in inbox_queries.visible_purposes(w.clinic_for(valid))
        ),
    ),
    Boundary(
        "apps.comms.inbox_queries.is_clinician",
        "comms",
        CLINICIANS,
        lambda w, valid: inbox_queries.is_clinician(w.clinic_for(valid)),
    ),
    Boundary(
        "apps.comms.inbox_queries.care_team_patients",
        "comms",
        CLINICIANS,
        _care_team,
    ),
    Boundary(
        "apps.comms.inbox_feed.question_items",
        "comms",
        CLINICIANS,
        _waiting_questions,
    ),
)
