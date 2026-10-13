"""Native workflow designer inputs; references remain in POST bodies."""

from typing import assert_never

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.identity.models import UserClinicRole
from apps.workflows.forms import KIND_CHOICES, REFERENCE_CHOICES
from apps.workflows.handlers import Handler
from apps.workflows.validation import MAX_SECONDS


class DefinitionForm(forms.Form):
    """Create a custom definition or an appointment/discharge checklist."""

    key = forms.SlugField(label=_("Workflow key"), max_length=64)
    template = forms.ChoiceField(
        label=_("Starting point"),
        choices=(
            ("custom", _("Custom task workflow")),
            ("appointment-discharge", _("Appointment and discharge checklist")),
        ),
    )
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput)

    def steps(self) -> list[dict[str, object]]:
        """Build the validated starting point using the frozen flat grammar."""
        result: list[dict[str, object]] = [
            {
                "handler": "task",
                "kind": "checklist",
                "owner_role": "receptionist",
                "subject": 0,
                "due_seconds": 3600,
            }
        ]
        if self.cleaned_data["template"] == "appointment-discharge":
            result.extend(
                [
                    {"handler": "timer", "seconds": 60},
                    {
                        "handler": "task",
                        "kind": "follow_up",
                        "owner_role": "receptionist",
                        "subject": 0,
                        "due_seconds": 86400,
                    },
                ]
            )
        return result


class StepForm(forms.Form):
    """Add one typed step without requiring users to author JSON."""

    handler = forms.ChoiceField(
        label=_("Step type"),
        choices=(
            ("task", _("Task")),
            ("timer", _("Timer")),
            ("external", _("Synthetic external action")),
        ),
    )
    task_kind = forms.ChoiceField(label=_("Task kind"), choices=KIND_CHOICES)
    owner_role = forms.ChoiceField(
        label=_("Responsible role"),
        choices=tuple(UserClinicRole.Role.choices),
    )
    subject = forms.IntegerField(label=_("Reference index"), min_value=0, max_value=49)
    seconds = forms.IntegerField(
        label=_("Timer seconds"), min_value=0, max_value=MAX_SECONDS
    )
    offset = forms.IntegerField(
        label=_("Task due in seconds"), min_value=0, max_value=MAX_SECONDS
    )

    def step(self) -> dict[str, object]:
        """Parse a validated step choice into its exact machine-consumed shape."""
        values = self.cleaned_data
        match Handler(values["handler"]):
            case Handler.TASK:
                return {
                    "handler": "task",
                    "kind": values["task_kind"],
                    "owner_role": values["owner_role"],
                    "subject": values["subject"],
                    "due_seconds": values["offset"],
                }
            case Handler.TIMER:
                return {"handler": "timer", "seconds": values["seconds"]}
            case Handler.EXTERNAL:
                return {"handler": "external", "provider": "workflow-synthetic-v1"}
            case unreachable:
                assert_never(unreachable)


class RunForm(forms.Form):
    """Start a pinned published version using a scoped typed reference."""

    definition_version_id = forms.UUIDField(
        label=_("Published version"), widget=forms.Select
    )
    subject_kind = forms.ChoiceField(
        label=_("Reference type"), choices=REFERENCE_CHOICES
    )
    subject_id = forms.UUIDField(label=_("Reference identifier"))
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput)


class RunSelectorForm(forms.Form):
    """Parse a versioned run command before any domain write."""

    run_id = forms.UUIDField(widget=forms.HiddenInput)
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    idempotency_key = forms.UUIDField(required=False, widget=forms.HiddenInput)


class VersionSelectorForm(forms.Form):
    """Parse a version command and its replay key before any write."""

    definition_id = forms.UUIDField(widget=forms.HiddenInput)
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput)
