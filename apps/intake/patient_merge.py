"""Explicit forward contacts/preferences/access, with no inferred verification."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.core.integration import hold_subject_mutation_key, hold_subject_mutation_lock
from apps.identity.current_context import current_actor_id, current_actor_username
from apps.intake.contacts import (
    SUBJECT_TYPE_PREFERENCE,
    _patient_channel_lock_key,
    _record_event,
)
from apps.intake.duplicate_detection import refresh_duplicate_candidates
from apps.intake.merge_transfer import (
    ForwardLink,
    merge_transfer_scope,
    record_forward_link,
)
from apps.intake.models import (
    PatientAccessGrant,
    PatientChannelPreference,
    PatientContact,
    PatientContactEvent,
    PatientSession,
)
from apps.intake.patient_access import (
    INVITATION_TTL,
    SECRET_BYTES,
    IssuedInvitation,
    _secret_hash,
)

if TYPE_CHECKING:
    from uuid import UUID


def transfer_patient_forward(
    *, clinic_id: UUID, merge_decision_id: UUID
) -> tuple[IssuedInvitation, ...]:
    """Preserve old bindings and require fresh verification and access secrets."""
    scope = merge_transfer_scope(
        clinic_id=clinic_id, merge_decision_id=merge_decision_id
    )
    actor_id = current_actor_id()
    actor_label = current_actor_username()
    invitations: list[IssuedInvitation] = []
    with transaction.atomic():
        # Contacts belong to the organization, not to each enrolled clinic.
        if clinic_id == scope.decision.clinic_id:
            for old in PatientContact.objects.filter(
                patient_id=scope.original.patient_id
            ).order_by("channel"):
                hold_subject_mutation_key(
                    _patient_channel_lock_key(scope.original.patient_id, old.channel)
                )
                hold_subject_mutation_key(
                    _patient_channel_lock_key(scope.survivor.patient_id, old.channel)
                )
                successor, created = PatientContact.objects.get_or_create(
                    organization_id=scope.clinic.organization_id,
                    patient_id=scope.survivor.patient_id,
                    channel=old.channel,
                    defaults={"destination": old.destination},
                )
                record_forward_link(
                    scope,
                    ForwardLink(
                        "contact",
                        old.pk,
                        successor.pk,
                        "recreated" if created else "kept_survivor",
                    ),
                )
                if created:
                    _record_event(
                        clinic=scope.clinic,
                        patient_id=scope.survivor.patient_id,
                        actor_id=actor_id,
                        actor_label=actor_label,
                        event_type=PatientContactEvent.EventType.CONTACT_SAVED,
                        contact=successor,
                        version=1,
                    )
                    record_phase1_event(
                        "intake.contact.saved",
                        clinic_id=clinic_id,
                        affected_record_id=successor.pk,
                    )
        preferences = (
            PatientChannelPreference.objects.select_for_update()
            .filter(clinic_id=clinic_id, patient_id=scope.original.patient_id)
            .order_by("id")
        )
        for preference in preferences:
            hold_subject_mutation_lock(SUBJECT_TYPE_PREFERENCE, preference.pk)
            hold_subject_mutation_key(
                _patient_channel_lock_key(scope.original.patient_id, preference.channel)
            )
            if preference.opted_in:
                preference.opted_in = False
                preference.version += 1
                preference.save(update_fields=("opted_in", "version", "updated_at"))
                _record_event(
                    clinic=scope.clinic,
                    patient_id=scope.original.patient_id,
                    actor_id=actor_id,
                    actor_label=actor_label,
                    event_type=PatientContactEvent.EventType.PREFERENCE_OPTED_OUT,
                    preference=preference,
                    version=preference.version,
                )
                record_phase1_event(
                    "intake.preference.opted_out",
                    clinic_id=clinic_id,
                    affected_record_id=preference.pk,
                )
            successor_preference, created = (
                PatientChannelPreference.objects.get_or_create(
                    organization_id=scope.clinic.organization_id,
                    clinic_id=clinic_id,
                    patient_id=scope.survivor.patient_id,
                    purpose=preference.purpose,
                    channel=preference.channel,
                    defaults={"opted_in": False},
                )
            )
            record_forward_link(
                scope,
                ForwardLink(
                    "preference",
                    preference.pk,
                    successor_preference.pk,
                    "recreated" if created else "kept_survivor",
                ),
            )
        grants = (
            PatientAccessGrant.objects.select_for_update()
            .filter(
                clinic_id=clinic_id,
                patient_id=scope.original.patient_id,
                revoked_at__isnull=True,
            )
            .order_by("id")
        )
        for grant in grants:
            grant.revoked_at = timezone.now()
            grant.save(update_fields=("revoked_at",))
            PatientSession.objects.filter(grant=grant, revoked_at__isnull=True).update(
                revoked_at=timezone.now()
            )
            secret = secrets.token_urlsafe(SECRET_BYTES)
            successor_grant = PatientAccessGrant.objects.create(
                organization_id=scope.clinic.organization_id,
                clinic_id=clinic_id,
                patient_id=scope.survivor.patient_id,
                enrollment=scope.survivor,
                issued_by_id=actor_id,
                issued_by_label=actor_label,
                secret_hash=_secret_hash(secret),
                operations=list(grant.operations),
                expires_at=timezone.now() + INVITATION_TTL,
            )
            invitations.append(IssuedInvitation(successor_grant, secret))
            record_forward_link(
                scope,
                ForwardLink(
                    "access_grant",
                    grant.pk,
                    successor_grant.pk,
                    "revoked_and_reissued",
                ),
            )
            record_phase1_event(
                "intake.patient_access.revoked",
                clinic_id=clinic_id,
                affected_record_id=grant.pk,
            )
            record_phase1_event(
                "intake.patient_access.issued",
                clinic_id=clinic_id,
                affected_record_id=successor_grant.pk,
            )
        refresh_duplicate_candidates(
            clinic_id=clinic_id, patient_id=scope.survivor.patient_id
        )
    return tuple(invitations)
