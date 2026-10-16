"""Immutable inputs, RP authority and resource/service publication."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils.translation import gettext_lazy as _

from apps.audit.services import record_phase1_event
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import Clinic
from apps.scheduling.access import AppointmentAccessDeniedError
from apps.scheduling.locks import acquire_advisory_locks, clinic_lock_key
from apps.scheduling.models import Resource, ServiceType

if TYPE_CHECKING:
    from datetime import date, time
    from uuid import UUID

MAX_NAME: Final = 120
MAX_CAPACITY: Final = 64
MAX_DURATION: Final = 720
MAX_BUFFER: Final = 240
MAX_PRICE_REF: Final = 80


@dataclass(frozen=True, slots=True)
class ResourceInput:
    """Immutable resource definition."""

    name: str
    kind: str
    capacity: int = 1


@dataclass(frozen=True, slots=True)
class ServiceInput:
    """Service requirements, never a clinical free-text field."""

    name: str
    duration_min: int
    buffer_before: int = 0
    buffer_after: int = 0
    required_professional_roles: tuple[str, ...] = ("physician",)
    required_resource_kinds: tuple[str, ...] = ()
    insurer_billable: bool = False
    price_ref: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class TemplateInput:
    """One local interval on selected weekdays; dates are inclusive."""

    weekdays: tuple[int, ...]
    start_local: time
    end_local: time
    valid_from: date
    valid_to: date
    practitioner_id: UUID | None = None
    resource_id: UUID | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ClosureInput:
    """A clinic closure or one subject absence with a closed reason code."""

    start_local: str
    end_local: str
    reason: str
    practitioner_id: UUID | None = None
    resource_id: UUID | None = None


def configuration_clinic(clinic_id: UUID) -> Clinic:
    """Authorize scheduling managers, including narrowed permission bundles."""
    try:
        try:
            require_permission("appointment.book", clinic_id=clinic_id)
        except CurrentActorError:
            require_permission("configuration.organization", clinic_id=clinic_id)
        return Clinic.objects.get(pk=clinic_id)
    except (CurrentActorError, Clinic.DoesNotExist) as error:
        raise AppointmentAccessDeniedError from error


def _locked_clinic(clinic_id: UUID) -> Clinic:
    configuration_clinic(clinic_id)
    acquire_advisory_locks((clinic_lock_key(clinic_id),))
    return configuration_clinic(clinic_id)


def _validate_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > MAX_NAME
        or any(char in name for char in "<>\n\r")
    ):
        raise ValidationError(
            _("Enter a plain scheduling name of at most 120 characters.")
        )


def create_resource(*, clinic_id: UUID, content: ResourceInput) -> Resource:
    """Publish a resource with immutable capacity."""
    _validate_name(content.name)
    if (
        content.kind not in Resource.Kind.values
        or type(content.capacity) is not int
        or not 1 <= content.capacity <= MAX_CAPACITY
    ):
        raise ValidationError(_("Choose a resource kind and capacity from 1 to 64."))
    with transaction.atomic():
        clinic = _locked_clinic(clinic_id)
        row = Resource.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            name=content.name.strip(),
            kind=content.kind,
            capacity=content.capacity,
        )
        record_phase1_event(
            "scheduling.resource.created",
            clinic_id=clinic_id,
            affected_record_id=row.pk,
        )
        return row


def create_service_type(*, clinic_id: UUID, content: ServiceInput) -> ServiceType:
    """Publish immutable duration, buffers and required resource kinds."""
    _validate_name(content.name)
    if (
        type(content.duration_min) is not int
        or not 1 <= content.duration_min <= MAX_DURATION
        or any(
            type(value) is not int or not 0 <= value <= MAX_BUFFER
            for value in (content.buffer_before, content.buffer_after)
        )
        or not content.required_professional_roles
        or not set(content.required_professional_roles)
        <= {"physician", "nurse", "allied_professional"}
        or not set(content.required_resource_kinds) <= set(Resource.Kind.values)
        or not isinstance(content.price_ref, str)
        or len(content.price_ref) > MAX_PRICE_REF
        or type(content.insurer_billable) is not bool
    ):
        raise ValidationError(
            _("Enter valid service duration, buffers and requirements.")
        )
    with transaction.atomic():
        clinic = _locked_clinic(clinic_id)
        row = ServiceType.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            name=content.name.strip(),
            duration_min=content.duration_min,
            buffer_before=content.buffer_before,
            buffer_after=content.buffer_after,
            required_professional_roles=sorted(
                set(content.required_professional_roles)
            ),
            required_resource_kinds=sorted(set(content.required_resource_kinds)),
            insurer_billable=content.insurer_billable,
            price_ref=content.price_ref,
        )
        record_phase1_event(
            "scheduling.service.created", clinic_id=clinic_id, affected_record_id=row.pk
        )
        return row


def _subject(
    clinic: Clinic, practitioner_id: UUID | None, resource_id: UUID | None
) -> None:
    if (
        resource_id is not None
        and not Resource.objects.filter(
            pk=resource_id, clinic=clinic, active=True
        ).exists()
    ):
        raise AppointmentAccessDeniedError
    if practitioner_id is not None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM "
                "clinic_app.scheduling_service_practitioners(%s) WHERE user_id=%s)",
                [clinic.pk, practitioner_id],
            )
            if cursor.fetchone() != (True,):
                raise AppointmentAccessDeniedError
