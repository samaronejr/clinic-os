"""Body-only review selections and version-bound demographic choices."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.intake.demographics import DEMOGRAPHICS_FIELD_VALUES
from apps.intake.duplicates import DemographicFieldChoice
from apps.intake.forms import (
    DEMOGRAPHIC_FIELD_LABELS,
    GENDER_IDENTITY_LABELS,
    SEX_AT_BIRTH_LABELS,
)

if TYPE_CHECKING:
    from django.http import QueryDict

    from apps.intake.demographics import DemographicsProfile
    from apps.intake.models import DuplicateCandidate


class DuplicateSelectionForm(forms.Form):
    """Select a candidate or decision only in an authenticated POST body."""

    candidate_id = forms.UUIDField(widget=forms.HiddenInput(), required=False)
    merge_decision_id = forms.UUIDField(widget=forms.HiddenInput(), required=False)
    reason = forms.CharField(
        label=_("Reason for the decision"),
        required=False,
        max_length=512,
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class DuplicateReviewForm(forms.Form):
    """Bind every chosen field to the exact source version displayed."""

    candidate_id = forms.UUIDField(widget=forms.HiddenInput())
    survivor_patient_id = forms.ChoiceField(label=_("Surviving registration"))
    expected_a = forms.IntegerField(min_value=0, widget=forms.HiddenInput())
    expected_b = forms.IntegerField(min_value=0, widget=forms.HiddenInput())
    reason = forms.CharField(
        label=_("Reason for the decision"),
        max_length=512,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(
        self,
        *,
        candidate: DuplicateCandidate,
        first: DemographicsProfile,
        second: DemographicsProfile,
        data: QueryDict | None = None,
    ) -> None:
        """Present real values from both registrations without inferring a choice."""
        super().__init__(data=data)
        self.profiles = (first, second)
        self.fields["survivor_patient_id"] = forms.ChoiceField(
            label=_("Surviving registration"),
            choices=(
                ("", _("Choose a registration")),
                *tuple(
                    (str(profile.patient_id), profile.display_name)
                    for profile in self.profiles
                ),
            ),
        )
        self.initial.update(
            {
                "candidate_id": candidate.pk,
                "expected_a": first.version,
                "expected_b": second.version,
            }
        )
        for field in DEMOGRAPHICS_FIELD_VALUES:
            labels = (
                SEX_AT_BIRTH_LABELS
                if field == "sex_at_birth"
                else GENDER_IDENTITY_LABELS
                if field == "gender_identity"
                else {}
            )
            choices = []
            for profile in self.profiles:
                value = labels.get(
                    profile.values[field], profile.values[field] or _("Not informed")
                )
                choices.append(
                    (str(profile.patient_id), f"{profile.display_name}: {value}")
                )
            self.fields[f"choice_{field}"] = forms.ChoiceField(
                label=DEMOGRAPHIC_FIELD_LABELS[field],
                choices=(("", _("Choose a registration")), *choices),
            )

    def field_choices(self) -> dict[str, DemographicFieldChoice]:
        """Return validated sources and their human-reviewed version numbers."""
        versions = {
            self.profiles[0].patient_id: int(self.cleaned_data["expected_a"]),
            self.profiles[1].patient_id: int(self.cleaned_data["expected_b"]),
        }
        return {
            field: DemographicFieldChoice(
                patient_id=(source := UUID(self.cleaned_data[f"choice_{field}"])),
                expected_version=versions[source],
            )
            for field in DEMOGRAPHICS_FIELD_VALUES
        }
