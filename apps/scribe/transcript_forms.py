"""Bounded native transcript commands; clinical selectors stay in POST bodies."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.scribe.transcript_contracts import MAX_TEXT

if TYPE_CHECKING:
    from uuid import UUID

SPEAKER_CHOICES = (
    ("unknown", _("Unknown speaker")),
    ("clinician", _("Clinician")),
    ("patient", _("Patient")),
    ("caregiver", _("Caregiver")),
)


class TranscriptValues(TypedDict):
    """The exact value types Django's declared fields produce after validation."""

    encounter_id: UUID
    session_id: UUID | None
    action: str
    expected_revision: int | None
    segment_id: UUID | None
    speaker: str
    text: str
    acknowledge_incomplete: bool


class TranscriptCommand(forms.Form):
    """Validate commands without ever granting authority from the submitted ids."""

    encounter_id = forms.UUIDField()
    session_id = forms.UUIDField(required=False)
    action = forms.ChoiceField(
        choices=(
            ("open", "open"),
            ("refresh", "refresh"),
            ("queue", "queue"),
            ("relabel", "relabel"),
            ("correct", "correct"),
            ("finalize", "finalize"),
        )
    )
    expected_revision = forms.IntegerField(min_value=0, required=False)
    segment_id = forms.UUIDField(required=False)
    speaker = forms.ChoiceField(choices=SPEAKER_CHOICES, required=False)
    text = forms.CharField(max_length=MAX_TEXT, required=False, strip=False)
    acknowledge_incomplete = forms.BooleanField(required=False)
