"""Clinic-scoped configuration, deterministic generation and opaque refusals."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID, uuid5

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.audit.services import record_phase1_event
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import Clinic
from apps.scheduling.access import AppointmentAccessDeniedError
from apps.scheduling.appointment_persistence import _constraint_name
from apps.scheduling.locks import (
    acquire_advisory_locks,
    clinic_lock_key,
    resource_lock_keys,
    user_lock_keys,
)
from apps.scheduling.models import (
    Absence,
    AvailabilityBlock,
    AvailabilityTemplate,
    Holiday,
    Resource,
    ServiceType,
)
from apps.scheduling.resource_errors import SchedulingRuleError
from apps.scheduling.timezones import parse_local_minute

if TYPE_CHECKING:
    from datetime import datetime


MAX_NAME: Final = 120
MAX_CAPACITY: Final = 64
MAX_DURATION: Final = 720
MAX_BUFFER: Final = 240
MAX_PRICE_REF: Final = 80
LAST_WEEKDAY: Final = 6
MAX_GENERATION_DAYS: Final = 366


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


def create_template(*, clinic_id: UUID, content: TemplateInput) -> AvailabilityTemplate:
    """Capture clinic timezone once; the generator never reads a wall clock."""
    if (
        (content.practitioner_id is None) == (content.resource_id is None)
        or not content.weekdays
        or any(
            type(day) is not int or not 0 <= day <= LAST_WEEKDAY
            for day in content.weekdays
        )
        or content.valid_from > content.valid_to
        or content.start_local >= content.end_local
        or any(
            value.second or value.microsecond or value.tzinfo is not None
            for value in (content.start_local, content.end_local)
        )
    ):
        raise ValidationError(
            _("Choose one template subject and a valid local weekly interval.")
        )
    with transaction.atomic():
        clinic = _locked_clinic(clinic_id)
        _subject(clinic, content.practitioner_id, content.resource_id)
        if not isinstance(clinic.timezone, str):
            raise AppointmentAccessDeniedError
        row = AvailabilityTemplate.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            practitioner_id=content.practitioner_id,
            resource_id=content.resource_id,
            weekdays=sorted(set(content.weekdays)),
            start_local=content.start_local,
            end_local=content.end_local,
            valid_from=content.valid_from,
            valid_to=content.valid_to,
            timezone=clinic.timezone,
        )
        record_phase1_event(
            "scheduling.template.created",
            clinic_id=clinic_id,
            affected_record_id=row.pk,
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


def create_closure(*, clinic_id: UUID, content: ClosureInput) -> Holiday | Absence:
    """Record a holiday or scoped absence; occupied windows cannot be hidden."""
    with transaction.atomic():
        clinic = _locked_clinic(clinic_id)
        if not isinstance(clinic.timezone, str):
            raise AppointmentAccessDeniedError
        start_at = parse_local_minute(content.start_local, clinic.timezone)
        end_at = parse_local_minute(content.end_local, clinic.timezone)
        absence = content.practitioner_id is not None or content.resource_id is not None
        choices = Absence.Reason.values if absence else Holiday.Reason.values
        if (
            end_at <= start_at
            or content.reason not in choices
            or (content.practitioner_id is not None and content.resource_id is not None)
        ):
            raise ValidationError(
                _("Choose a positive closure interval and a listed reason.")
            )
        _subject(clinic, content.practitioner_id, content.resource_id)
        try:
            with transaction.atomic():
                if absence:
                    row: Holiday | Absence = Absence.objects.create(
                        organization_id=clinic.organization_id,
                        clinic_id=clinic_id,
                        practitioner_id=content.practitioner_id,
                        resource_id=content.resource_id,
                        start_at=start_at,
                        end_at=end_at,
                        reason=content.reason,
                    )
                else:
                    row = Holiday.objects.create(
                        organization_id=clinic.organization_id,
                        clinic_id=clinic_id,
                        start_at=start_at,
                        end_at=end_at,
                        reason=content.reason,
                    )
        except IntegrityError as error:
            if _constraint_name(error) == "scheduling_resource_conflict":
                msg = "resource_conflict"
                raise SchedulingRuleError(msg) from error
            raise
        record_phase1_event(
            "scheduling.closure.created", clinic_id=clinic_id, affected_record_id=row.pk
        )
        return row


def _template(clinic: Clinic, template_id: UUID) -> AvailabilityTemplate:
    try:
        return AvailabilityTemplate.objects.get(pk=template_id, clinic=clinic)
    except AvailabilityTemplate.DoesNotExist as error:
        raise AppointmentAccessDeniedError from error


def generate_availability(
    *, clinic_id: UUID, template_id: UUID, start_date: date, end_date: date
) -> tuple[UUID, ...]:
    """Idempotent job body; bounded explicit dates make retries clock-independent."""
    if end_date < start_date or (end_date - start_date).days >= MAX_GENERATION_DAYS:
        raise ValidationError(_("Generate at most one year of availability at a time."))
    with transaction.atomic():
        clinic = _locked_clinic(clinic_id)
        template = _template(clinic, template_id)
        acquire_advisory_locks(
            (
                *user_lock_keys(
                    ()
                    if template.practitioner_id is None
                    else (template.practitioner_id,)
                ),
                *resource_lock_keys(
                    () if template.resource_id is None else (template.resource_id,)
                ),
            )
        )
        _subject(clinic, template.practitioner_id, template.resource_id)
        if not template.active:
            msg = "outside_template"
            raise SchedulingRuleError(msg)
        result = []
        day = max(start_date, template.valid_from)
        while day <= min(end_date, template.valid_to):
            if day.weekday() in template.weekdays:
                result.append(_generate_day(template, day))
            if day == min(end_date, template.valid_to):
                break
            day += timedelta(days=1)
        return tuple(result)


def _generate_day(template: AvailabilityTemplate, day: date) -> UUID:
    existing = AvailabilityBlock.objects.filter(
        template=template, generated_date=day
    ).first()
    if existing is not None:
        return existing.pk
    starts = parse_local_minute(
        f"{day.isoformat()}T{template.start_local:%H:%M}", template.timezone
    )
    ends = parse_local_minute(
        f"{day.isoformat()}T{template.end_local:%H:%M}", template.timezone
    )
    key = uuid5(template.pk, day.isoformat())
    try:
        with transaction.atomic():
            row = AvailabilityBlock.objects.create(
                organization_id=template.organization_id,
                clinic_id=template.clinic_id,
                practitioner_id=template.practitioner_id,
                resource_id=template.resource_id,
                template=template,
                generated_date=day,
                start_at=starts,
                end_at=ends,
                idempotency_key=key,
                create_fingerprint=hashlib.sha256(
                    b"clinic-template-day-v1\0" + key.bytes
                ).digest(),
            )
    except IntegrityError as error:
        if _constraint_name(error) in {
            "scheduling_availability_resource_excl",
            "scheduling_availability_active_practitioner_excl",
        }:
            msg = "outside_template"
            raise SchedulingRuleError(msg) from error
        raise
    record_phase1_event(
        "scheduling.availability.created",
        clinic_id=template.clinic_id,
        affected_record_id=row.pk,
    )
    return row.pk


def retire_definition(
    *,
    clinic_id: UUID,
    kind: Literal["resource", "service", "template", "holiday", "absence"],
    record_id: UUID,
) -> None:
    """One-way retirement retains definitions, reservations and generated history."""
    models: dict[
        str,
        type[Resource | ServiceType | AvailabilityTemplate | Holiday | Absence],
    ] = {
        "resource": Resource,
        "service": ServiceType,
        "template": AvailabilityTemplate,
        "holiday": Holiday,
        "absence": Absence,
    }
    model = models[kind]
    with transaction.atomic():
        clinic = _locked_clinic(clinic_id)
        row = model.objects.filter(pk=record_id, clinic=clinic).first()
        if row is None:
            raise AppointmentAccessDeniedError
        if not row.active:
            return
        block_ids = (
            tuple(
                AvailabilityBlock.objects.filter(
                    template_id=row.pk, retired_at__isnull=True
                ).values_list("pk", flat=True)
            )
            if kind == "template"
            else ()
        )
        row.active = False
        try:
            with transaction.atomic():
                row.save(update_fields=("active",))
        except IntegrityError as error:
            if _constraint_name(error) == "scheduling_resource_conflict":
                code = "resource_conflict"
                raise SchedulingRuleError(code) from error
            raise
        for block_id in block_ids:
            record_phase1_event(
                "scheduling.availability.retired",
                clinic_id=clinic_id,
                affected_record_id=block_id,
            )
        record_phase1_event(
            "scheduling.definition.retired",
            clinic_id=clinic_id,
            affected_record_id=row.pk,
        )


def require_open_window(
    *,
    clinic_id: UUID,
    practitioner_id: UUID,
    resource_ids: tuple[UUID, ...],
    start_at: datetime,
    end_at: datetime,
) -> None:
    """Give an early translated refusal; the database independently rechecks."""
    if (
        Holiday.objects.filter(
            clinic_id=clinic_id, active=True, start_at__lt=end_at, end_at__gt=start_at
        ).exists()
        or Absence.objects.filter(
            clinic_id=clinic_id, active=True, start_at__lt=end_at, end_at__gt=start_at
        )
        .filter(Q(practitioner_id=practitioner_id) | Q(resource_id__in=resource_ids))
        .exists()
    ):
        msg = "holiday"
        raise SchedulingRuleError(msg)
