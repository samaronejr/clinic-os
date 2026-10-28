"""Bound every POST scalar; note selection and all clinical text stay in bodies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from django import forms

from apps.ehr.forms import SECTION_LABELS
from apps.scribe.draft_contracts import SECTIONS, DraftInputError

if TYPE_CHECKING:
    from django.http import QueryDict


@dataclass(frozen=True, slots=True)
class ReviewValues:
    """Parsed command data is never authority."""

    encounter_id: UUID
    version_id: UUID
    action: str
    expected_revision: int | None
    transcript_id: UUID | None
    request_key: UUID | None
    command_id: UUID | None
    section: str | None
    suggestion_id: UUID | None
    content: dict[str, str]
    replacement: str | None


class ReviewCommand(forms.Form):
    """One native form preserves unsaved manual edits across every action."""

    encounter_id = forms.UUIDField(widget=forms.HiddenInput)
    version_id = forms.UUIDField(widget=forms.HiddenInput)
    transcript_id = forms.UUIDField(required=False, widget=forms.HiddenInput)
    expected_revision = forms.IntegerField(
        required=False, min_value=1, widget=forms.HiddenInput
    )
    request_key = forms.UUIDField(required=False, widget=forms.HiddenInput)
    command_id = forms.UUIDField(required=False, widget=forms.HiddenInput)
    action = forms.ChoiceField(
        required=False,
        choices=tuple(
            (value, value)
            for value in (
                "open",
                "refresh",
                "generate",
                "save",
                *(f"regenerate_{section}" for section in SECTIONS),
            )
        ),
    )
    decision = forms.CharField(required=False, max_length=64)

    def __init__(
        self, data: QueryDict | None = None, *, initial: dict[str, object] | None = None
    ) -> None:
        """Dynamic replacement fields remain bounded and never become selectors."""
        super().__init__(data=data, initial=initial)
        for section in SECTIONS:
            self.fields[section] = forms.CharField(
                label=SECTION_LABELS[section],
                required=False,
                strip=False,
                max_length=20_000,
                widget=forms.Textarea(
                    attrs={"rows": 6, "autocomplete": "off", "spellcheck": "false"}
                ),
            )
        if data is None:
            return
        replacements = [key for key in data if key.startswith("replacement-")]
        if len(replacements) > len(SECTIONS):
            raise DraftInputError
        for key in replacements:
            try:
                if str(UUID(key.removeprefix("replacement-"))) != key.removeprefix(
                    "replacement-"
                ):
                    raise DraftInputError
            except ValueError as error:
                raise DraftInputError from error
            self.fields[key] = forms.CharField(
                required=False, strip=False, max_length=20_000
            )
        if set(data) - {*self.fields, "csrfmiddlewaretoken"} or any(
            len(data.getlist(key)) != 1 for key in data
        ):
            raise DraftInputError

    def _decision(self) -> tuple[str, UUID | None, str | None]:
        data = self.cleaned_data
        action = data["action"]
        decision = data["decision"]
        suggestion = None
        replacement = None
        if decision:
            if action:
                raise DraftInputError
            action, separator, identifier = decision.partition(":")
            if not separator or action not in {"accepted", "modified", "rejected"}:
                raise DraftInputError
            try:
                suggestion = UUID(identifier)
            except ValueError as error:
                raise DraftInputError from error
            if action == "modified":
                name = f"replacement-{suggestion}"
                if name not in data:
                    raise DraftInputError
                replacement = data[name]
        return action, suggestion, replacement

    def values(self) -> ReviewValues:
        """Require every command-specific precondition before a service call."""
        data = self.cleaned_data
        action, suggestion, replacement = self._decision()
        section = None
        if action.startswith("regenerate_"):
            section = action.removeprefix("regenerate_")
            action = "generate"
        if action not in {"open", "refresh"} and data["expected_revision"] is None:
            raise DraftInputError
        if action == "generate" and (
            data["transcript_id"] is None or data["request_key"] is None
        ):
            raise DraftInputError
        if suggestion is not None and data["command_id"] is None:
            raise DraftInputError
        if not action:
            raise DraftInputError
        return ReviewValues(
            data["encounter_id"],
            data["version_id"],
            action,
            data["expected_revision"],
            data["transcript_id"],
            data["request_key"],
            data["command_id"],
            section,
            suggestion,
            {key: data[key] for key in SECTIONS},
            replacement,
        )
