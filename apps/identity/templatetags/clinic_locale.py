"""Display-only formats: never use these helpers for persistence or parsing input."""

from datetime import datetime
from decimal import Decimal

from django import template
from django.utils.formats import date_format, number_format
from django.utils.translation import gettext_lazy as _

register = template.Library()

APPOINTMENT_LABELS = {
    "scheduled": _("Scheduled"),
    "cancelled": _("Cancelled"),
    "patient_request": _("Patient request"),
    "clinic_request": _("Clinic request"),
    "practitioner_unavailable": _("Practitioner unavailable"),
    "duplicate": _("Duplicate"),
    "other": _("Other"),
}


@register.filter
def clinic_minute(value: str) -> str:
    """Format an already converted clinic-local ISO minute, without re-zoning."""
    minute = datetime.strptime(value, "%Y-%m-%dT%H:%M")  # noqa: DTZ007 - civil display only
    return str(date_format(minute, "SHORT_DATETIME_FORMAT"))


@register.filter
def appointment_label(value: str) -> str:
    """Translate the closed appointment vocabulary only at the display boundary."""
    return str(APPOINTMENT_LABELS[value])


@register.filter
def brl(value: Decimal) -> str:
    """Format a decimal amount in reais, without accepting user input or floats."""
    return "R$ " + number_format(value, decimal_pos=2, force_grouping=True)


@register.simple_tag
def brl_example() -> str:
    """Synthetic format sample; there is no billing workflow in this release."""
    return brl(Decimal("1234.56"))
