"""Translate stored labels without changing the reported clinical values."""

from datetime import date

from django import template
from django.utils.translation import gettext as _

from apps.orders.values import CATALOG

register = template.Library()


@register.filter
def examination_label(value: str) -> str:
    """Name a closed local catalog entry."""
    return str(CATALOG[value])


@register.filter
def source_flag_label(value: str) -> str:
    """Label the source's explicit flag, without deciding a new flag."""
    return {
        "": _("Not provided"),
        "normal": _("Normal"),
        "low": _("Low"),
        "high": _("High"),
        "critical": _("Critical"),
    }[value]


@register.filter
def source_birth_date(value: str) -> str:
    """Format a validated source birthday or its explicitly missing state."""
    return (
        date.fromisoformat(value).strftime("%d/%m/%Y") if value else _("Not provided")
    )
