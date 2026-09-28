"""Consent trigger oracles: fire the guarded INSERT, never call the trigger.

``clinic_app.consent_guard`` checks todo 6's ``has_permission`` for the four
staff branches. Expected role sets come from ``BUNDLES_V1``, never from the
SQL body. Professional actors receive a registration and care-team membership
first, so a refusal isolates the bundle rather than a missing scope.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import timedelta
from typing import TYPE_CHECKING

import psycopg
from apps.consent.models import (
    AIUseDisclosure,
    ConsentText,
    NoticeVersion,
    ParticipantAcknowledgment,
)
from apps.ehr.models import Encounter
from apps.identity.models import CareTeamMembership, ProfessionalRegistration
from apps.identity.permissions import BUNDLES_V1
from django.db import DatabaseError
from django.utils import timezone

from identity.legacy_owner_boundaries import _owner_call

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from identity.sql_guard_probes import SqlWorld

COUNCIL = {"physician": "CRM", "nurse": "COREN", "allied_professional": "CRP"}
PUBLISH = ("configuration.clinic", "configuration.organization")
CLINICAL = ("clinical.write",)
TEXT = "Sintetico SQL oracle"


def permitted(*permissions: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            role
            for role, bundle in BUNDLES_V1.items()
            if any(permission in bundle for permission in permissions)
        )
    )


def grant_registration(organization: UUID, clinic: UUID, user: UUID, role: str) -> None:
    """A current synthetic professional registration in the clinic's UF (SP)."""
    now = timezone.now()
    ProfessionalRegistration.objects.get_or_create(
        organization_id=organization,
        clinic_id=clinic,
        user_id=user,
        role=role,
        defaults={
            "council": COUNCIL[role],
            "number": "SINTETICO-020",
            "jurisdiction": "SP",
            "specialty": "Sintetico",
            "status": "regular",
            "valid_from": now - timedelta(days=1),
            "valid_to": now + timedelta(days=1),
        },
    )


def grant_professional_scope(
    organization: UUID, clinic: UUID, user: UUID, role: str, enrollment: UUID
) -> None:
    """Registration plus care-team membership, as todo 6 scope requires."""
    if role not in COUNCIL:
        return
    grant_registration(organization, clinic, user, role)
    now = timezone.now()
    CareTeamMembership.objects.get_or_create(
        organization_id=organization,
        clinic_id=clinic,
        patient_enrollment_id=enrollment,
        user_id=user,
        role=role,
        defaults={
            "valid_from": now - timedelta(days=1),
            "valid_to": now + timedelta(days=1),
        },
    )


def _scope(w: SqlWorld) -> None:
    actor = w.actor
    _owner_call(
        lambda: grant_professional_scope(
            actor.graph.organization_a,
            actor.clinic,
            actor.actor.pk,
            actor.role,
            w.operational.enrollment,
        )
    )


@contextmanager
def trigger_refusal() -> Iterator[None]:
    """A 42501 must come from consent_guard, the first layer, not from RLS.

    The insert policies repeat the same names; without this, removing the
    trigger's permission check would still pass on the policy's 42501.
    """
    try:
        yield
    except DatabaseError as error:
        cause = error.__cause__
        if getattr(cause, "sqlstate", None) == "42501":
            assert isinstance(cause, psycopg.Error)
            assert cause.diag.message_primary == "consent staff authority required"
        raise


def insert_text(w: SqlWorld, valid: bool) -> bool:
    clinic = w.actor.clinic_for(valid)
    version = _owner_call(
        lambda: ConsentText.objects.filter(
            clinic_id=clinic, purpose="marketing"
        ).count()
    )
    assert isinstance(version, int)
    with trigger_refusal():
        ConsentText.objects.create(
            organization_id=w.actor.graph.organization_a,
            clinic_id=clinic,
            purpose="marketing",
            version=version + 1,
            text=TEXT,
            language="pt-BR",
            digest=hashlib.sha256(TEXT.encode()).hexdigest(),
            published_by_id=w.actor.actor.pk,
        )
    return True


def insert_notice(w: SqlWorld, valid: bool) -> bool:
    clinic = w.actor.clinic_for(valid)
    version = _owner_call(
        lambda: NoticeVersion.objects.filter(clinic_id=clinic, topic="ai_use").count()
    )
    assert isinstance(version, int)
    with trigger_refusal():
        NoticeVersion.objects.create(
            organization_id=w.actor.graph.organization_a,
            clinic_id=clinic,
            topic="ai_use",
            version=version + 1,
            text=TEXT,
            language="pt-BR",
            digest=hashlib.sha256(TEXT.encode()).hexdigest(),
            published_by_id=w.actor.actor.pk,
        )
    return True


def insert_participant(w: SqlWorld, valid: bool) -> bool:
    _scope(w)
    with trigger_refusal():
        ParticipantAcknowledgment.objects.create(
            organization_id=w.actor.graph.organization_a,
            clinic_id=w.actor.clinic_for(valid),
            session_id=w.actor.encounter,
            participant_kind="interpreter",
            acknowledged_by_clinician_id=w.actor.actor.pk,
            acknowledged_at=timezone.now(),
        )
    return True


def _undisclosed_encounter(w: SqlWorld) -> Encounter:
    """An encounter of the world's patient that holds no disclosure yet."""
    encounter = (
        Encounter.objects.filter(
            clinic_id=w.actor.clinic,
            patient_id=w.actor.appointment.patient_id,
            aiusedisclosure__isnull=True,
        )
        .order_by("pk")
        .first()
    )
    assert encounter is not None
    return encounter


def insert_disclosure(w: SqlWorld, valid: bool) -> bool:
    _scope(w)
    encounter = _owner_call(lambda: _undisclosed_encounter(w))
    assert isinstance(encounter, Encounter)
    with trigger_refusal():
        AIUseDisclosure.objects.create(
            organization_id=w.actor.graph.organization_a,
            clinic_id=w.actor.clinic_for(valid),
            encounter_id=encounter.pk,
            patient_id=encounter.patient_id,
            informed=True,
            refused=True,
            recorded_by_id=w.actor.actor.pk,
            recorded_at=timezone.now(),
        )
    return True
