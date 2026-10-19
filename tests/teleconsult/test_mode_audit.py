"""Each actual participant mode transition appends privacy-safe audit history."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.audit.canonical import AUDIT_PAYLOAD_ALLOWED
from apps.audit.models import AuditEvent
from apps.audit.services import verify_chain
from apps.intake.patient_access import (
    issue_invitation,
    patient_session_context,
    redeem_invitation,
)
from apps.teleconsult import participants
from apps.teleconsult.models import TeleconsultEvent
from apps.tenancy.db import tenant_context

from patient_service_support import runtime_role
from renewal.test_encounters import setup_context
from renewal.test_teleconsult_sessions import synthetic_provider
from teleconsult.test_participants import World, seed_world

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

__all__ = ("synthetic_provider",)
pytestmark = pytest.mark.django_db(transaction=True)
MODE_EVENTS = ("teleconsult.audio_only.enabled", "teleconsult.audio_only.disabled")


def mode_audits(organization: UUID, before: int) -> list[AuditEvent]:
    with setup_context(organization):
        return list(
            AuditEvent.objects.filter(organization_id=organization).order_by("seq")
        )[before:]


def audit_count(organization: UUID) -> int:
    with setup_context(organization):
        return AuditEvent.objects.filter(organization_id=organization).count()


def second_patient_session(w: World) -> UUID:
    """Redeem an ordinary invitation for the same enrolled patient."""
    with runtime_role(), tenant_context(w.graph.shared_user, w.organization):
        invitation = issue_invitation(clinic_id=w.clinic, enrollment_id=w.enrollment)
    with runtime_role():
        second = redeem_invitation(w.clinic, invitation.secret)
    assert second is not None
    assert second != w.patient_session
    return second


def test_physician_mode_transitions_append_registered_audit_without_duplicates(
    rbac_graph: RbacGraph, synthetic_provider: object
) -> None:
    w = seed_world(rbac_graph)
    with setup_context(w.organization):
        before = AuditEvent.objects.filter(organization_id=w.organization).count()
    with runtime_role(), tenant_context(w.graph.physician, w.organization):
        # Repeating the current mode is not a transition and must not audit.
        for enabled in (True, True, False, False):
            participants.set_audio_only(
                clinic_id=w.clinic, session_id=w.session, enabled=enabled
            )
    with setup_context(w.organization):
        kinds = list(
            TeleconsultEvent.objects.filter(
                session_id=w.session,
                actor_role="physician",
                kind__in=("audio_only", "video_restored"),
            )
            .order_by("created_at", "pk")
            .values_list("kind", flat=True)
        )
        added = list(
            AuditEvent.objects.filter(organization_id=w.organization).order_by("seq")
        )[before:]
        assert kinds == ["audio_only", "video_restored"]
        assert [audit.event_type for audit in added] == list(MODE_EVENTS)
        for audit, kind in zip(added, kinds, strict=True):
            assert audit.actor_user_id == w.graph.physician
            assert audit.affected_record_type == "teleconsult.session"
            assert audit.affected_record_id == str(w.session)
            assert audit.payload == {"clinic_id": str(w.clinic), "object_verb": kind}
            assert set(audit.payload) <= AUDIT_PAYLOAD_ALLOWED
        verify_chain(w.organization)


def test_patient_mode_transitions_append_registered_audit_once(
    rbac_graph: RbacGraph, synthetic_provider: object
) -> None:
    w = seed_world(rbac_graph)
    before = audit_count(w.organization)
    with runtime_role():
        for enabled in (True, True, False, False):
            with patient_session_context(w.patient_session):
                participants.set_patient_audio_only(
                    session_id=w.session, enabled=enabled
                )
    added = mode_audits(w.organization, before)
    assert [audit.event_type for audit in added] == list(MODE_EVENTS)
    for audit, kind in zip(added, ("audio_only", "video_restored"), strict=True):
        assert audit.actor_user_id == w.patient_session
        assert audit.affected_record_type == "teleconsult.session"
        assert audit.affected_record_id == str(w.session)
        assert audit.payload == {"clinic_id": str(w.clinic), "object_verb": kind}
    with setup_context(w.organization):
        verify_chain(w.organization)
