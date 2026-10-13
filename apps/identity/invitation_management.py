"""Exact-clinic pending invitation terminal transitions with retained history."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models.functions import Now

from apps.audit.services import record_phase1_event
from apps.identity.lifecycle_authority import (
    LifecycleDeniedError,
    require_staff_management,
)
from apps.identity.models import StaffInvitation

if TYPE_CHECKING:
    from uuid import UUID


def expire_pending_invitations(*, clinic_id: UUID) -> None:
    """Use PostgreSQL time, never a caller clock, for persisted expiry."""
    require_staff_management(clinic_id=clinic_id)
    with transaction.atomic():
        expired = StaffInvitation.objects.select_for_update().filter(
            clinic_id=clinic_id,
            state=StaffInvitation.State.PENDING,
            expires_at__lte=Now(),
        )
        for invitation in expired:
            invitation.state = StaffInvitation.State.EXPIRED
            invitation.save(update_fields=("state",))
            record_phase1_event(
                "identity.staff.invitation_expired",
                clinic_id=clinic_id,
                affected_record_id=invitation.pk,
            )


def revoke_invitation(*, clinic_id: UUID, invitation_id: UUID) -> None:
    """Idempotently revoke only a currently scoped pending invitation."""
    require_staff_management(clinic_id=clinic_id)
    with transaction.atomic():
        invitation = (
            StaffInvitation.objects.select_for_update()
            .filter(pk=invitation_id, clinic_id=clinic_id)
            .first()
        )
        if invitation is None:
            raise LifecycleDeniedError
        if invitation.state == StaffInvitation.State.REVOKED:
            return
        if invitation.state != StaffInvitation.State.PENDING:
            raise LifecycleDeniedError
        StaffInvitation.objects.filter(pk=invitation.pk).update(
            state=StaffInvitation.State.REVOKED, revoked_at=Now()
        )
        record_phase1_event(
            "identity.staff.invitation_revoked",
            clinic_id=clinic_id,
            affected_record_id=invitation.pk,
        )
