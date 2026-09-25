"""Native Settings forms for resource scheduling definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.scheduling.appointment_forms import LocalMinuteField
from apps.scheduling.models import Absence, Holiday, Resource

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.http import QueryDict


class ResourceForm(forms.Form):
    """Publish a named fixed-capacity pool."""

    name = forms.CharField(label=_("Name"), max_length=120)
    kind = forms.ChoiceField(label=_("Resource kind"), choices=Resource.Kind.choices)
    capacity = forms.IntegerField(
        label=_("Capacity"), min_value=1, max_value=64, initial=1
    )


class ServiceForm(forms.Form):
    """Publish duration and requirements; no financial amount is inferred."""

    name = forms.CharField(label=_("Name"), max_length=120)
    duration_min = forms.IntegerField(
        label=_("Duration (minutes)"), min_value=1, max_value=720
    )
    buffer_before = forms.IntegerField(
        label=_("Buffer before (minutes)"), min_value=0, max_value=240, initial=0
    )
    buffer_after = forms.IntegerField(
        label=_("Buffer after (minutes)"), min_value=0, max_value=240, initial=0
    )
    required_professional_roles = forms.MultipleChoiceField(
        label=_("Required professional roles"),
        choices=[
            ("physician", _("Physician")),
            ("nurse", _("Nurse")),
            ("allied_professional", _("Allied professional")),
        ],
        initial=["physician"],
    )
    required_resource_kinds = forms.MultipleChoiceField(
        label=_("Required resource kinds"),
        choices=Resource.Kind.choices,
        required=False,
    )
    insurer_billable = forms.BooleanField(label=_("Insurer billable"), required=False)
    price_ref = forms.CharField(
        label=_("Price reference"), max_length=80, required=False
    )


class SubjectForm(forms.Form):
    """A subject selector populated only from the authorized clinic."""

    practitioner_id = forms.TypedChoiceField(
        label=_("Professional"), required=False, empty_value=None
    )
    resource_id = forms.TypedChoiceField(
        label=_("Resource"), required=False, empty_value=None
    )

    def __init__(
        self,
        data: QueryDict | None = None,
        *,
        practitioners: Sequence[tuple[str, str]],
        resources: Sequence[tuple[str, str]],
        prefix: str | None = None,
    ) -> None:
        """Keep foreign selectors out of both GET and validation choices."""
        super().__init__(data=data, prefix=prefix)
        for name, choices in (
            ("practitioner_id", practitioners),
            ("resource_id", resources),
        ):
            field = self.fields[name]
            if isinstance(field, forms.ChoiceField):
                field.choices = [("", _("Not selected")), *choices]


class TemplateForm(SubjectForm):
    """One weekly local interval, without a caller-controlled timezone."""

    weekdays = forms.TypedMultipleChoiceField(
        label=_("Weekdays"),
        coerce=int,
        choices=[
            (0, _("Monday")),
            (1, _("Tuesday")),
            (2, _("Wednesday")),
            (3, _("Thursday")),
            (4, _("Friday")),
            (5, _("Saturday")),
            (6, _("Sunday")),
        ],
    )
    start_local = forms.TimeField(
        label=_("Start time"),
        input_formats=["%H:%M"],
        widget=forms.TimeInput(attrs={"type": "time", "step": 60}),
    )
    end_local = forms.TimeField(
        label=_("End time"),
        input_formats=["%H:%M"],
        widget=forms.TimeInput(attrs={"type": "time", "step": 60}),
    )
    valid_from = forms.DateField(
        label=_("Valid from"), widget=forms.DateInput(attrs={"type": "date"})
    )
    valid_to = forms.DateField(
        label=_("Valid to"), widget=forms.DateInput(attrs={"type": "date"})
    )


class ClosureForm(SubjectForm):
    """An empty subject closes the clinic; a selected subject records absence."""

    start_local = LocalMinuteField(label=_("Starts (clinic local)"))
    end_local = LocalMinuteField(label=_("Ends (clinic local)"))
    reason = forms.ChoiceField(
        label=_("Reason"), choices=[*Holiday.Reason.choices, *Absence.Reason.choices]
    )


class GenerateForm(forms.Form):
    """An explicit bounded date window makes job retries deterministic."""

    template_id = forms.UUIDField(
        label=_("Availability template"), widget=forms.Select()
    )
    start_date = forms.DateField(
        label=_("Generate from"), widget=forms.DateInput(attrs={"type": "date"})
    )
    end_date = forms.DateField(
        label=_("Generate through"), widget=forms.DateInput(attrs={"type": "date"})
    )


class RetireForm(forms.Form):
    """Only closed definition kinds and body-carried identifiers."""

    kind = forms.ChoiceField(
        choices=[
            (kind, kind)
            for kind in ("resource", "service", "template", "holiday", "absence")
        ],
        widget=forms.HiddenInput(),
    )
    record_id = forms.UUIDField(widget=forms.HiddenInput())
