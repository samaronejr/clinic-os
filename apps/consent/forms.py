"""Native explicit choices; consent is never preselected."""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.consent.models import ConsentPurpose, NoticeTopic, ParticipantKind
from apps.consent.services import MAX_TEXT


class AcceptanceForm(forms.Form):
    """A session-bound displayed version and a required deliberate choice."""

    offer = forms.CharField(widget=forms.HiddenInput)
    purpose = forms.ChoiceField(
        choices=ConsentPurpose.choices, widget=forms.HiddenInput
    )
    accepted = forms.BooleanField(
        label=_("I read this version's text and I consent."), required=True
    )


class RefusalForm(forms.Form):
    """A session-bound displayed version and a deliberate refusal."""

    offer = forms.CharField(widget=forms.HiddenInput)
    purpose = forms.ChoiceField(
        choices=ConsentPurpose.choices, widget=forms.HiddenInput
    )


class TextForm(forms.Form):
    """Publish a new overlay without editing any prior accepted text."""

    purpose = forms.ChoiceField(label="Finalidade", choices=ConsentPurpose.choices)
    text = forms.CharField(
        label="Texto integral (pt-BR)",
        strip=False,
        max_length=MAX_TEXT,
        widget=forms.Textarea(attrs={"rows": 10}),
    )


class NoticeForm(forms.Form):
    """Publish an information-only notice version; it authorizes nothing."""

    topic = forms.ChoiceField(label=_("Topic"), choices=NoticeTopic.choices)
    text = forms.CharField(
        label=_("Full notice text (pt-BR)"),
        strip=False,
        max_length=MAX_TEXT,
        widget=forms.Textarea(attrs={"rows": 10}),
    )


class DisclosureForm(forms.Form):
    """Record the per-encounter AI-use attestation once, honestly."""

    encounter_id = forms.UUIDField(label=_("Encounter"))
    informed = forms.BooleanField(
        label=_("I informed the patient that AI may assist in this encounter."),
        required=True,
    )
    refused = forms.BooleanField(
        label=_("The patient refused AI assistance."), required=False
    )


class AcknowledgmentForm(forms.Form):
    """Record the recording notice delivered to one non-patient voice."""

    session_id = forms.UUIDField(label=_("Clinical session"))
    participant_kind = forms.ChoiceField(
        label=_("Participant"), choices=ParticipantKind.choices
    )
