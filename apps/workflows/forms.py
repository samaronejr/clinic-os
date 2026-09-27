"""Native forms; every record selector remains in a POST body."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from django import forms
from django.utils.translation import gettext_lazy as _

KIND_CHOICES = (
    ("checklist", _("Checklist")),
    ("review", _("Review")),
    ("follow_up", _("Follow-up")),
)
PRIORITY_CHOICES = (
    ("low", _("Low")),
    ("normal", _("Normal")),
    ("high", _("High")),
    ("urgent", _("Urgent")),
)
STATE_CHOICES = (
    ("open", _("Open")),
    ("assigned", _("Assigned")),
    ("in_progress", _("In progress")),
    ("done", _("Done")),
    ("cancelled", _("Cancelled")),
)
REFERENCE_CHOICES = (
    ("clinic", _("Clinic")),
    ("enrollment", _("Patient enrollment")),
    ("appointment", _("Appointment")),
    ("task", _("Task")),
)


class CreateTaskForm(forms.Form):
    """Create an operational work item without collecting clinical narrative."""

    kind = forms.ChoiceField(label=_("Task kind"), choices=KIND_CHOICES)
    priority = forms.ChoiceField(
        label=_("Priority"), choices=PRIORITY_CHOICES, initial="normal"
    )
    due_date = forms.DateField(label=_("Due date and time"), input_formats=["%d/%m/%Y"])
    due_time = forms.TimeField(label=_("Time"), input_formats=["%H:%M"])
    subject_kind = forms.ChoiceField(
        label=_("Reference type"), choices=REFERENCE_CHOICES, initial="clinic"
    )
    subject_id = forms.UUIDField(label=_("Reference"))
    depends_on = forms.UUIDField(label=_("Depends on task"), required=False)
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput, initial=uuid4)

    def clean(self) -> dict[str, Any]:
        """Validate a pt-BR wall time in the caller's explicit clinic timezone."""
        values = super().clean() or {}
        if "due_date" in values and "due_time" in values:
            values["due_at"] = forms.DateTimeField().clean(
                datetime.combine(values["due_date"], values["due_time"])
            )
        return values


class TaskSelectorForm(forms.Form):
    """Bind an action to a row revision without placing it in a URL."""

    task_id = forms.UUIDField(widget=forms.HiddenInput)
    expected_revision = forms.IntegerField(
        min_value=1, max_value=2_147_483_647, widget=forms.HiddenInput
    )


class CommentSelectorForm(TaskSelectorForm):
    """Require a stable command identifier for an append-only comment."""

    idempotency_key = forms.UUIDField(widget=forms.HiddenInput)


class TaskFilterForm(forms.Form):
    """Bounded, nonclinical filter vocabulary."""

    state = forms.ChoiceField(
        label=_("Status"),
        choices=(("", _("All statuses")), *STATE_CHOICES),
        required=False,
    )
    priority = forms.ChoiceField(
        label=_("Priority"),
        choices=(("", _("All priorities")), *PRIORITY_CHOICES),
        required=False,
    )
    kind = forms.ChoiceField(
        label=_("Task kind"),
        choices=(("", _("All kinds")), *KIND_CHOICES),
        required=False,
    )
