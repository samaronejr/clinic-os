"""Serializers that adapt service frozen dataclasses; never models."""

from __future__ import annotations

import datetime
from typing import Any, Final

from rest_framework import serializers

from apps.core.api.errors import ERROR_CODES
from apps.scheduling.agenda_queries import AgendaPage


class ErrorSerializer(serializers.Serializer[None]):
    """The ``{code, message_key}`` refusal body shared by every endpoint."""

    code = serializers.ChoiceField(choices=[(code, code) for code in ERROR_CODES])
    message_key = serializers.CharField()


# Supported civil-date window and page bound: every accepted value maps to
# representable UTC bounds and a bigint-safe SQL offset (25 rows per page).
AGENDA_FIRST_DATE: Final = datetime.date(2000, 1, 1)
AGENDA_LAST_DATE: Final = datetime.date(2199, 12, 31)
AGENDA_MAX_PAGE: Final = 10_000


class AgendaQueryRequestSerializer(serializers.Serializer[None]):
    """POST body of ``agenda/query``; the clinic identifier stays in the body."""

    clinic_id = serializers.UUIDField()
    view = serializers.ChoiceField(choices=(("day", "day"), ("week", "week")))
    date = serializers.DateField(
        help_text=(
            f"Clinic-local civil date from {AGENDA_FIRST_DATE.isoformat()} "
            f"to {AGENDA_LAST_DATE.isoformat()}."
        )
    )
    page = serializers.IntegerField(min_value=1, max_value=AGENDA_MAX_PAGE, default=1)

    def validate_date(self, value: datetime.date) -> datetime.date:
        """Refuse civil dates outside the supported agenda window."""
        if not AGENDA_FIRST_DATE <= value <= AGENDA_LAST_DATE:
            message = "date outside the supported agenda window"
            raise serializers.ValidationError(message)
        return value


class AgendaItemSerializer(serializers.Serializer[Any]):
    """Adapt ``scheduling.AgendaItem``: one clinic-local appointment row."""

    appointment_id = serializers.UUIDField(read_only=True)
    patient_display_name = serializers.CharField(read_only=True)
    practitioner_id = serializers.UUIDField(read_only=True)
    practitioner_display_identifier = serializers.CharField(read_only=True)
    start_local = serializers.CharField(read_only=True)
    end_local = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)


class AgendaPageSerializer(serializers.Serializer[AgendaPage]):
    """Adapt ``scheduling.AgendaPage``: one bounded agenda page."""

    items = AgendaItemSerializer(many=True, read_only=True)
    view = serializers.ChoiceField(
        choices=(("day", "day"), ("week", "week")), read_only=True
    )
    date = serializers.DateField(read_only=True)
    page = serializers.IntegerField(read_only=True)
    total = serializers.IntegerField(read_only=True)
    page_count = serializers.IntegerField(read_only=True)
    start_at = serializers.DateTimeField(read_only=True)
    end_at = serializers.DateTimeField(read_only=True)
