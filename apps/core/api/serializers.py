"""Serializers that adapt service frozen dataclasses; never models."""

from __future__ import annotations

import datetime
from typing import Any, Final

from rest_framework import serializers

from apps.core.api.errors import ERROR_CODES
from apps.core.command_search import MAX_COMMAND_QUERY
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


class CommandSearchRequestSerializer(serializers.Serializer[None]):
    """POST body of ``command/search``; search text never travels in a URL.

    ``clinic_id`` is optional: without it the shell's current clinic is used.
    An unknown or foreign clinic answers exactly like a query with no match.
    """

    q = serializers.CharField(
        max_length=MAX_COMMAND_QUERY, allow_blank=True, trim_whitespace=False
    )
    clinic_id = serializers.UUIDField(required=False)
    page_path = serializers.RegexField(
        r"^/[A-Za-z0-9/_.-]*$",
        max_length=200,
        required=False,
        help_text="Path of the page the palette was opened on (never a record).",
    )


class CommandResultSerializer(serializers.Serializer[Any]):
    """One allowed palette row.

    ``token`` stands for a record selector (a patient) held server-side in
    the session; POST it as ``token`` to ``action_url_name``. ``href`` is the
    state-free path of a destination or action and is null for tokenized
    rows. ``label``/``meta`` of a patient row are the name and age only.
    """

    kind = serializers.ChoiceField(
        choices=(
            ("destination", "destination"),
            ("action", "action"),
            ("saved_view", "saved_view"),
            ("save_view", "save_view"),
            ("archive_view", "archive_view"),
            ("patient", "patient"),
        ),
        read_only=True,
    )
    meta = serializers.CharField(read_only=True)
    action_url_name = serializers.CharField(read_only=True)
    token = serializers.CharField(read_only=True, allow_null=True)
    href = serializers.CharField(read_only=True, allow_null=True)

    def get_fields(self) -> dict[str, serializers.Field[Any, Any, Any, Any]]:
        """Add ``label`` here: as a class attribute it would shadow ``Field.label``."""
        fields = super().get_fields()
        fields["label"] = serializers.CharField(read_only=True)
        return fields
