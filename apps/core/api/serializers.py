"""Serializers that adapt service frozen dataclasses; never models."""

from __future__ import annotations

import datetime
from typing import Any, Final

from rest_framework import serializers

from apps.core.api.errors import ERROR_CODES
from apps.core.command_search import MAX_COMMAND_QUERY
from apps.scheduling.agenda_board import BOARD_VIEWS, AgendaBoard
from apps.scheduling.agenda_queries import AGENDA_STATUSES, AgendaPage
from apps.workflows.inbox import (
    DUE_FILTERS,
    MAX_KNOWN_KEYS,
    OWNER_FILTERS,
    InboxPage,
)


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


class AgendaBoardFiltersSerializer(serializers.Serializer[None]):
    """Optional board filters; unknown catalog entries answer ``invalid_input``."""

    professional_id = serializers.UUIDField(required=False)
    resource_id = serializers.UUIDField(required=False)
    service_type_id = serializers.UUIDField(required=False)
    status = serializers.ChoiceField(
        choices=[(status, status) for status in AGENDA_STATUSES], required=False
    )


class AgendaBoardQueryRequestSerializer(serializers.Serializer[None]):
    """POST body of ``agenda/board``: the range is the view containing ``date``.

    ``clinic_id`` is optional: without it the shell's current clinic is used.
    Record selectors never appear here; filters name only catalog entries.
    """

    clinic_id = serializers.UUIDField(required=False)
    view = serializers.ChoiceField(choices=[(view, view) for view in BOARD_VIEWS])
    date = serializers.DateField(
        help_text=(
            f"Clinic-local civil date from {AGENDA_FIRST_DATE.isoformat()} "
            f"to {AGENDA_LAST_DATE.isoformat()}; the day, ISO week or month "
            "containing it."
        )
    )
    filters = AgendaBoardFiltersSerializer(required=False)

    def validate_date(self, value: datetime.date) -> datetime.date:
        """Refuse civil dates outside the supported agenda window."""
        if not AGENDA_FIRST_DATE <= value <= AGENDA_LAST_DATE:
            message = "date outside the supported agenda window"
            raise serializers.ValidationError(message)
        return value


class BoardLaneSerializer(serializers.Serializer[Any]):
    """Adapt ``scheduling.BoardLane``: a professional, room or equipment lane."""

    kind = serializers.CharField(read_only=True)
    lane_id = serializers.UUIDField(read_only=True)

    def get_fields(self) -> dict[str, serializers.Field[Any, Any, Any, Any]]:
        """Add ``label`` here: as a class attribute it would shadow ``Field.label``."""
        fields = super().get_fields()
        fields["label"] = serializers.CharField(read_only=True)
        return fields


class BoardAppointmentSerializer(serializers.Serializer[Any]):
    """Adapt ``scheduling.BoardAppointment``; no birth date or contact data."""

    appointment_id = serializers.UUIDField(read_only=True)
    patient_display_name = serializers.CharField(read_only=True)
    practitioner_id = serializers.UUIDField(read_only=True)
    resource_ids = serializers.ListField(child=serializers.UUIDField(), read_only=True)
    service_type_id = serializers.UUIDField(read_only=True, allow_null=True)
    start_local = serializers.CharField(read_only=True)
    end_local = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    revision = serializers.IntegerField(read_only=True)
    movable = serializers.BooleanField(read_only=True)
    actions = serializers.ListField(child=serializers.CharField(), read_only=True)
    hold_until = serializers.CharField(read_only=True)
    hold_expiring = serializers.BooleanField(read_only=True)


class OccupancySerializer(serializers.Serializer[Any]):
    """Adapt ``scheduling.Occupancy``: a busy interval, other units unnamed."""

    lane_id = serializers.UUIDField(read_only=True)
    start_local = serializers.CharField(read_only=True)
    end_local = serializers.CharField(read_only=True)
    other_unit = serializers.BooleanField(read_only=True)


class DayCountSerializer(serializers.Serializer[Any]):
    """Adapt ``scheduling.DayCount``: appointments starting on one date."""

    day = serializers.DateField(read_only=True)
    total = serializers.IntegerField(read_only=True)


class AgendaBoardSerializer(serializers.Serializer[AgendaBoard]):
    """Adapt ``scheduling.AgendaBoard``: slots and appointments of one view."""

    view = serializers.ChoiceField(
        choices=[(view, view) for view in BOARD_VIEWS], read_only=True
    )
    anchor = serializers.DateField(read_only=True)
    first_day = serializers.DateField(read_only=True)
    last_day = serializers.DateField(read_only=True)
    lanes = BoardLaneSerializer(many=True, read_only=True)
    slots = serializers.ListField(child=serializers.CharField(), read_only=True)
    appointments = BoardAppointmentSerializer(many=True, read_only=True)
    occupancy = OccupancySerializer(many=True, read_only=True)
    counts = DayCountSerializer(many=True, read_only=True)
    can_move = serializers.BooleanField(read_only=True)
    can_book = serializers.BooleanField(read_only=True)
    truncated = serializers.BooleanField(read_only=True)


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
            ("notice", "notice"),
        ),
        read_only=True,
    )
    meta = serializers.CharField(read_only=True)
    action_url_name = serializers.CharField(read_only=True)
    token = serializers.CharField(read_only=True, allow_null=True)
    href = serializers.CharField(read_only=True, allow_null=True)
    html = serializers.CharField(
        read_only=True,
        help_text="Server-rendered, escaped option markup for the workspace combobox.",
    )

    def get_fields(self) -> dict[str, serializers.Field[Any, Any, Any, Any]]:
        """Add ``label`` here: as a class attribute it would shadow ``Field.label``."""
        fields = super().get_fields()
        fields["label"] = serializers.CharField(read_only=True)
        return fields


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


class AddendumOpenRequestSerializer(serializers.Serializer[None]):
    """POST body of ``ehr/addendum/open``; the encounter stays in the body."""

    clinic_id = serializers.UUIDField()
    encounter_id = serializers.UUIDField()


class AddendumOpenedSerializer(serializers.Serializer[Any]):
    """The caller's own addendum draft on that encounter."""

    addendum_id = serializers.UUIDField(read_only=True)
    revision = serializers.IntegerField(read_only=True)


class AddendumAutosaveRequestSerializer(serializers.Serializer[None]):
    """POST body of ``ehr/addendum/autosave``; same contract as the main draft."""

    clinic_id = serializers.UUIDField()
    addendum_id = serializers.UUIDField()
    expected_revision = serializers.IntegerField(
        min_value=1, max_value=AUTOSAVE_MAX_REVISION
    )
    editor_command_id = serializers.UUIDField(
        help_text="Idempotency key: a retry of the same save reuses it."
    )
    text = serializers.CharField(
        allow_blank=True, trim_whitespace=False, max_length=AUTOSAVE_MAX_SECTION
    )


class AddendumSavedSerializer(serializers.Serializer[Any]):
    """The acknowledged addendum save."""

    revision = serializers.IntegerField(read_only=True)
    saved_at = serializers.CharField(
        read_only=True, help_text="ISO 8601 in the clinic's UTC offset."
    )


class InboxQueryRequestSerializer(serializers.Serializer[None]):
    """POST body of ``inbox/query``; selectors and filters stay in the body.

    ``known`` lists the item keys the caller is showing; keys that are no
    longer visible come back in ``removed`` so the client can say so.
    """

    clinic_id = serializers.UUIDField(required=False)
    kinds = serializers.ListField(
        child=serializers.CharField(max_length=24), max_length=16, default=list
    )
    owner = serializers.ChoiceField(
        choices=[(value, value) for value in OWNER_FILTERS], default="all"
    )
    due = serializers.ChoiceField(
        choices=[(value, value) for value in DUE_FILTERS], default="all"
    )
    known = serializers.ListField(
        child=serializers.CharField(max_length=64),
        max_length=MAX_KNOWN_KEYS,
        default=list,
    )


class InboxObservationSerializer(serializers.Serializer[Any]):
    """Adapt ``workflows.InboxObservation``: exact source strings only."""

    code = serializers.CharField(read_only=True)
    examination = serializers.CharField(source="label", read_only=True)
    value = serializers.CharField(read_only=True)
    unit = serializers.CharField(read_only=True)
    reference_interval = serializers.CharField(read_only=True)
    flag = serializers.CharField(read_only=True)


class InboxItemSerializer(serializers.Serializer[Any]):
    """Adapt ``workflows.InboxItem``: one typed, owned and due inbox row."""

    key = serializers.CharField(read_only=True)
    kind = serializers.CharField(read_only=True)
    record_id = serializers.UUIDField(read_only=True)
    target_id = serializers.UUIDField(read_only=True, allow_null=True)
    revision = serializers.IntegerField(read_only=True)
    title = serializers.CharField(read_only=True)
    patient_label = serializers.CharField(read_only=True)
    owner = serializers.CharField(read_only=True)
    owner_label = serializers.CharField(read_only=True)
    due_at = serializers.DateTimeField(read_only=True, allow_null=True)
    overdue = serializers.BooleanField(read_only=True)
    priority = serializers.CharField(read_only=True)
    critical = serializers.BooleanField(read_only=True)
    action = serializers.CharField(read_only=True)
    bulk_eligible = serializers.BooleanField(read_only=True)
    preview = InboxObservationSerializer(many=True, read_only=True)


class InboxPageSerializer(serializers.Serializer[InboxPage]):
    """Adapt ``workflows.InboxPage``; unauthorized callers get it empty."""

    items = InboxItemSerializer(many=True, read_only=True)
    counts = serializers.DictField(child=serializers.IntegerField(), read_only=True)
    total = serializers.IntegerField(read_only=True)
    overdue = serializers.IntegerField(read_only=True)
    critical = serializers.IntegerField(read_only=True)
    removed = serializers.ListField(child=serializers.CharField(), read_only=True)
    truncated = serializers.BooleanField(read_only=True)
    counts_complete = serializers.BooleanField(read_only=True)
