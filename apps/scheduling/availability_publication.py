"""Immutable template/closure publication and scoped definition retirement."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils.translation import gettext_lazy as _

from apps.audit.services import record_phase1_event
from apps.scheduling.access import AppointmentAccessDeniedError
from apps.scheduling.appointment_persistence import _constraint_name
from apps.scheduling.models import (
    Absence,
    AvailabilityBlock,
    AvailabilityTemplate,
    Holiday,
    Resource,
    ServiceType,
)
from apps.scheduling.resource_definitions import (
    ClosureInput,
    TemplateInput,
    _locked_clinic,
    _subject,
)
from apps.scheduling.resource_errors import SchedulingRuleError
from apps.scheduling.timezones import parse_local_minute

if TYPE_CHECKING:
    from uuid import UUID

LAST_WEEKDAY: Final = 6


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
