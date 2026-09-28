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


# Plan item 27 autosave: every body scalar is bounded before the service sees it.
AUTOSAVE_MAX_REVISION: Final = 2_147_483_647
AUTOSAVE_MAX_SECTION: Final = 20_000


class SoapSectionsSerializer(serializers.Serializer[None]):
    """Exactly the four SOAP sections as plain text; never trimmed."""

    subjective = serializers.CharField(
        allow_blank=True, trim_whitespace=False, max_length=AUTOSAVE_MAX_SECTION
    )
    objective = serializers.CharField(
        allow_blank=True, trim_whitespace=False, max_length=AUTOSAVE_MAX_SECTION
    )
    assessment = serializers.CharField(
        allow_blank=True, trim_whitespace=False, max_length=AUTOSAVE_MAX_SECTION
    )
    plan = serializers.CharField(
        allow_blank=True, trim_whitespace=False, max_length=AUTOSAVE_MAX_SECTION
    )

    def to_internal_value(self, data: Any) -> dict[str, Any]:  # noqa: ANN401 - DRF hook
        """Refuse any key outside the four SOAP sections."""
        if isinstance(data, dict) and set(data) - set(self.fields):
            message = "unknown section"
            raise serializers.ValidationError(message)
        return dict(super().to_internal_value(data))


class DraftAutosaveRequestSerializer(serializers.Serializer[None]):
    """POST body of ``ehr/autosave``; record selectors stay in the body."""

    clinic_id = serializers.UUIDField()
    version_id = serializers.UUIDField()
    expected_revision = serializers.IntegerField(
        min_value=1, max_value=AUTOSAVE_MAX_REVISION
    )
    editor_command_id = serializers.UUIDField(
        help_text="Idempotency key: a retry of the same save reuses it."
    )
    editor_session = serializers.UUIDField(
        help_text="The editing tab; one tab holds the draft lock at a time."
    )
    sections = SoapSectionsSerializer()
    handover = serializers.BooleanField(
        default=False,
        help_text="Ask the tab holding the lock to hand it over after its next save.",
    )


class DraftAutosaveSavedSerializer(serializers.Serializer[Any]):
    """The acknowledged save: shown as saved only after this response."""

    revision = serializers.IntegerField(read_only=True)
    saved_at = serializers.CharField(
        read_only=True, help_text="ISO 8601 in the clinic's UTC offset."
    )
    lock = serializers.ChoiceField(
        choices=(("held", "held"), ("handed_over", "handed_over")), read_only=True
    )


class DraftDiffLineSerializer(serializers.Serializer[Any]):
    """One compared line; ``removed`` exists only in the saved text."""

    kind = serializers.ChoiceField(
        choices=(("same", "same"), ("added", "added"), ("removed", "removed")),
        read_only=True,
    )
    text = serializers.CharField(read_only=True)


class DraftSectionDiffSerializer(serializers.Serializer[Any]):
    """The saved text of one changed section and its comparison."""

    section = serializers.CharField(read_only=True)
    theirs = serializers.CharField(read_only=True)
    lines = DraftDiffLineSerializer(many=True, read_only=True)


class DraftAutosaveConflictSerializer(serializers.Serializer[Any]):
    """A stale revision: nothing was written; merge explicitly."""

    current_revision = serializers.IntegerField(read_only=True)
    diff = DraftSectionDiffSerializer(many=True, read_only=True)
