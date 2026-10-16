"""Resumable clinic onboarding over trusted startup declarations."""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import rfc8785
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.identity.cnpj import validate_cnpj
from apps.identity.current_context import (
    CurrentActorError,
    current_actor_id,
    require_permission,
)
from apps.identity.lifecycle_authority import require_staff_management
from apps.identity.models import (
    Clinic,
    ClinicConfiguration,
    OnboardingProgress,
    Organization,
    RecoveryBatch,
    StaffInvitation,
)
from apps.identity.onboarding_registry import (
    OnboardingStep,
    register_onboarding_step,
    registered_onboarding_steps,
)
from apps.providers.models import ProviderCapability
from apps.providers.services import current_version, is_live
from apps.scheduling.models import Appointment, AvailabilityBlock

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

CORE_STEP_KEYS: Final = (
    "organization_unit",
    "staff_invitations",
    "recovery_enrollment",
    "provider_connections",
    "opening_agenda",
)


class OnboardingCheckError(RuntimeError):
    """Refuse async, malformed or mutating completion checks."""


@dataclass(frozen=True, slots=True)
class StepProgress:
    """One authorized startup descriptor and its current server-side receipt."""

    key: str
    completed: bool
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """Governed status only; account, credentials and approval PII stay private."""

    key: str
    state: str
    live_eligible: bool

    @property
    def label(self) -> str:
        """Show the actual closed task4 state, not just a live-eligibility flag."""
        labels = {
            "waiting_external": _("Waiting for external setup"),
            "researched": _("Researched"),
            "selected_in_plan": _("Selected in plan"),
            "approved_to_test": _("Approved to test"),
            "sandbox": _("Sandbox"),
            "production_authorized": _("Production authorized"),
            "activated": _("Activated"),
            "degraded": _("Degraded"),
            "revoked": _("Revoked"),
        }
        return labels[self.state]


def provider_status(*, clinic_id: UUID) -> tuple[ProviderStatus, ...]:
    """Read the actual task4 capability status without enabling a provider."""
    require_staff_management(clinic_id=clinic_id)
    statuses: list[ProviderStatus] = []
    keys = (
        ProviderCapability.objects.filter(
            Q(clinic_id=clinic_id) | Q(clinic_id__isnull=True)
        )
        .order_by("key")
        .values_list("key", flat=True)
        .distinct()
    )
    for key in keys:
        version = current_version(key, clinic_id=clinic_id)
        statuses.append(
            ProviderStatus(
                key=key,
                state="waiting_external" if version is None else version.state,
                live_eligible=is_live(key, clinic_id=clinic_id),
            )
        )
    return tuple(statuses)


def _provider_fingerprint(*, clinic_id: UUID) -> bytes:
    statuses = provider_status(clinic_id=clinic_id)
    return hashlib.sha256(
        rfc8785.dumps(
            [
                {
                    "key": status.key,
                    "state": status.state,
                    "live": status.live_eligible,
                }
                for status in statuses
            ]
        )
    ).digest()


def _organization_unit(*, clinic_id: UUID) -> bool:
    clinic = Clinic.objects.get(pk=clinic_id)
    organization = Organization.objects.get(pk=clinic.organization_id)
    if (
        organization.status != Organization.Status.ACTIVE
        or not organization.name.strip()
        or not clinic.name.strip()
        or not clinic.crm_uf
        or not clinic.timezone
    ):
        return False
    try:
        validate_cnpj(organization.cnpj)
    except ValidationError:
        return False
    return ClinicConfiguration.objects.filter(clinic_id=clinic_id).exists()


def _staff_invitations(*, clinic_id: UUID) -> bool:
    return StaffInvitation.objects.filter(
        clinic_id=clinic_id,
        state=StaffInvitation.State.ACCEPTED,
        accepted_at__isnull=False,
        password_set_at__isnull=False,
        device_id__isnull=False,
    ).exists()


def _recovery_enrollment(*, clinic_id: UUID) -> bool:
    del clinic_id
    actor_id = current_actor_id()
    return RecoveryBatch.objects.filter(
        user_id=actor_id,
        revoked_at__isnull=True,
        device__confirmed=True,
    ).exists()


def _provider_connections(*, clinic_id: UUID) -> bool:
    return OnboardingProgress.objects.filter(
        clinic_id=clinic_id,
        step_key="provider_connections",
        reviewed_fingerprint=_provider_fingerprint(clinic_id=clinic_id),
    ).exists()


def _opening_agenda(*, clinic_id: UUID) -> bool:
    return (
        AvailabilityBlock.objects.filter(
            clinic_id=clinic_id, retired_at__isnull=True, end_at__gt=timezone.now()
        ).exists()
        and Appointment.objects.filter(
            clinic_id=clinic_id, status=Appointment.Status.SCHEDULED
        ).exists()
    )


def register_core_onboarding_steps() -> None:
    """Register exactly the five original15 steps during identity startup."""
    for key, check in (
        ("organization_unit", _organization_unit),
        ("staff_invitations", _staff_invitations),
        ("recovery_enrollment", _recovery_enrollment),
        ("provider_connections", _provider_connections),
        ("opening_agenda", _opening_agenda),
    ):
        register_onboarding_step(key, None, check)


def _completion(step: OnboardingStep, *, clinic_id: UUID) -> bool:
    """Run strict-bool checks in an always-rolled-back read-only savepoint."""
    result: object
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL transaction_read_only = on")
        result = step.completion_check(clinic_id=clinic_id)
        if inspect.iscoroutine(result):
            result.close()
            raise OnboardingCheckError
        if type(result) is not bool:
            raise OnboardingCheckError
        transaction.set_rollback(True)
    return result


def onboarding_progress(
    *, clinic_id: UUID, persist: bool = False
) -> tuple[StepProgress, ...]:
    """Filter current scope/permission before every real completion invocation."""
    _actor_id, clinic = require_staff_management(clinic_id=clinic_id)
    progress: list[StepProgress] = []
    for step in registered_onboarding_steps():
        if step.permission is not None:
            try:
                require_permission(step.permission, clinic_id=clinic_id)
            except CurrentActorError:
                continue
        completed = _completion(step, clinic_id=clinic_id)
        receipt = OnboardingProgress.objects.filter(
            clinic_id=clinic_id, step_key=step.key
        ).first()
        if persist:
            stamp = (
                receipt.completed_at
                if (
                    completed
                    and receipt is not None
                    and receipt.completed_at is not None
                )
                else timezone.now()
                if completed
                else None
            )
            receipt, _created = OnboardingProgress.objects.update_or_create(
                clinic_id=clinic_id,
                step_key=step.key,
                defaults={
                    "organization_id": clinic.organization_id,
                    "completed_at": stamp,
                },
            )
        progress.append(
            StepProgress(
                key=step.key,
                completed=completed,
                completed_at=None if receipt is None else receipt.completed_at,
            )
        )
    return tuple(progress)


def review_provider_status(*, clinic_id: UUID) -> tuple[StepProgress, ...]:
    """Record that the manager reviewed actual statuses, never that they are live."""
    _actor_id, clinic = require_staff_management(clinic_id=clinic_id)
    OnboardingProgress.objects.update_or_create(
        clinic_id=clinic_id,
        step_key="provider_connections",
        defaults={
            "organization_id": clinic.organization_id,
            "reviewed_fingerprint": _provider_fingerprint(clinic_id=clinic_id),
        },
    )
    return onboarding_progress(clinic_id=clinic_id, persist=True)
