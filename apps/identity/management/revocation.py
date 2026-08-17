"""Guarded owner-only clinic role revocation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.identity.management.base import LifecycleCommandError
from apps.identity.management.context import (
    LifecycleContext,
    assume_runtime_owner,
    scoped_owner_gucs,
)
from apps.identity.models import UserClinicRole
from apps.scheduling.locks import (
    acquire_advisory_locks,
    clinic_lock_key,
    user_lock_keys,
)
from apps.scheduling.models import Appointment, AvailabilityBlock

if TYPE_CHECKING:
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class RevokeStaffRoleRequest:
    """Select one exact target user and role assignment."""

    target_user_id: UUID
    role: UserClinicRole.Role


def revoke_staff_role(
    context: LifecycleContext,
    request: RevokeStaffRoleRequest,
) -> None:
    """Delete one guarded role and append its exact tenant event."""
    keys = (
        clinic_lock_key(context.clinic_id),
        *user_lock_keys((request.target_user_id,)),
    )
    with transaction.atomic(), scoped_owner_gucs(context):
        acquire_advisory_locks(keys)
        with assume_runtime_owner(context):
            pass
        role = (
            UserClinicRole.objects.select_for_update()
            .filter(
                organization_id=context.organization_id,
                clinic_id=context.clinic_id,
                user_id=request.target_user_id,
                role=request.role,
            )
            .first()
        )
        if role is None:
            raise LifecycleCommandError
        if request.role == UserClinicRole.Role.OWNER:
            another_owner_exists = (
                UserClinicRole.objects.filter(
                    organization_id=context.organization_id,
                    clinic_id=context.clinic_id,
                    role=UserClinicRole.Role.OWNER,
                    user__is_active=True,
                )
                .exclude(user_id=request.target_user_id)
                .values("user_id")
                .distinct()
                .exists()
            )
            if not another_owner_exists:
                raise LifecycleCommandError
        if request.role == UserClinicRole.Role.PHYSICIAN and (
            AvailabilityBlock.objects.filter(
                organization_id=context.organization_id,
                clinic_id=context.clinic_id,
                practitioner_id=request.target_user_id,
                retired_at__isnull=True,
            ).exists()
            or Appointment.objects.filter(
                organization_id=context.organization_id,
                clinic_id=context.clinic_id,
                practitioner_id=request.target_user_id,
                status=Appointment.Status.SCHEDULED,
                end_at__gt=timezone.now(),
            ).exists()
        ):
            raise LifecycleCommandError
        role_id = role.pk
        role.delete()
        record_phase1_event(
            "identity.staff_role.revoked",
            clinic_id=context.clinic_id,
            affected_record_id=role_id,
        )
