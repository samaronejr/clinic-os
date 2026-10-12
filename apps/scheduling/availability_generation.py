"""Idempotent promised availability and the matching closure predicate."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid5

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.audit.services import record_phase1_event
from apps.scheduling.access import AppointmentAccessDeniedError
from apps.scheduling.appointment_persistence import _constraint_name
from apps.scheduling.locks import (
    acquire_advisory_locks,
    resource_lock_keys,
    user_lock_keys,
)
from apps.scheduling.models import (
    Absence,
    AvailabilityBlock,
    AvailabilityTemplate,
    Holiday,
)
from apps.scheduling.resource_definitions import _locked_clinic, _subject
from apps.scheduling.resource_errors import SchedulingRuleError
from apps.scheduling.timezones import parse_local_minute

if TYPE_CHECKING:
    from datetime import datetime

    from apps.identity.models import Clinic

MAX_GENERATION_DAYS: Final = 366


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
