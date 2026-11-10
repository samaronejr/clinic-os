"""Native body-only governance forms; selectors never become continuation URLs."""

from uuid import uuid4

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.identity.models import UserClinicRole


class SsoConfigurationForm(forms.Form):
    """Only the configured deterministic issuer can run before external gates."""

    issuer = forms.URLField(label=_("Synthetic issuer"), assume_scheme="https")
    client_id = forms.CharField(label=_("Client identifier"), max_length=80)


class GroupMappingForm(forms.Form):
    """Approve a specific directory group and a specific unit's canonical role."""

    group_id = forms.UUIDField(label=_("Directory group ID"))
    unit_id = forms.UUIDField(label=_("Unit ID"))
    bundle = forms.ChoiceField(
        label=_("Role bundle"),
        choices=UserClinicRole.Role.choices,
        help_text=_(
            "Workflow v2 roles require prior owner issuance; "
            "directory claims never select a bundle version."
        ),
    )


class ConnectionCommandForm(forms.Form):
    """Keep even directory connection selectors in POST bodies."""

    connection_id = forms.UUIDField(label=_("Connection ID"))


class SupportApprovalForm(forms.Form):
    """Customer consent identifies a named support user, purpose and short expiry."""

    support_user_id = forms.UUIDField(label=_("Support account ID"))
    purpose = forms.CharField(
        label=_("Support purpose"), max_length=512, widget=forms.Textarea
    )
    minutes = forms.IntegerField(
        label=_("Access duration in minutes"), min_value=1, max_value=240, initial=30
    )


class TemplatePropagationForm(forms.Form):
    """Retries bind the immutable source version and target unit."""

    source_id = forms.UUIDField(label=_("Source template version ID"))
    unit_id = forms.UUIDField(label=_("Target unit ID"))
    command_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid4)


class UnitAccessForm(forms.Form):
    """Revoke actual membership rather than add a second policy authority."""

    unit_id = forms.UUIDField(label=_("Unit ID"))
    user_id = forms.UUIDField(label=_("Staff account ID"))
    command_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid4)


class EmergencyAccessForm(forms.Form):
    """Reason is encrypted; neither it nor the patient appears in a URL."""

    enrollment_id = forms.UUIDField(label=_("Patient enrollment ID"))
    encounter_id = forms.UUIDField(label=_("Encounter ID"), required=False)
    reason = forms.CharField(
        label=_("Emergency access reason"), max_length=2000, widget=forms.Textarea
    )
    minutes = forms.IntegerField(
        label=_("Access duration in minutes"), min_value=1, max_value=240, initial=30
    )


class GrantCommandForm(forms.Form):
    """Select a current grant in the body without redisplaying protected text."""

    grant_id = forms.UUIDField(widget=forms.HiddenInput)
