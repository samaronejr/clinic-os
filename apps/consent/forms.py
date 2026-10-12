"""Native explicit choices; consent is never preselected."""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.consent.models import ConsentPurpose, NoticeTopic, ParticipantKind
from apps.consent.services import MAX_TEXT


class AcceptanceForm(forms.Form):
    """A session-bound displayed version and an explicit acceptance or refusal."""

    offer = forms.CharField(widget=forms.HiddenInput)
    purpose = forms.ChoiceField(
        choices=ConsentPurpose.choices, widget=forms.HiddenInput
    )
    accepted = forms.BooleanField(
        label=_("I have read this version and consent to this purpose."),
        required=False,
    )


class TextForm(forms.Form):
    """Publish a new overlay without editing any prior accepted text."""

    purpose = forms.ChoiceField(label=_("Purpose"), choices=ConsentPurpose.choices)
    text = forms.CharField(
        label=_("Full text (pt-BR)"),
        strip=False,
        max_length=MAX_TEXT,
        widget=forms.Textarea(attrs={"rows": 10}),
    )


class NoticeForm(forms.Form):
    """Publish information independently of every consent decision."""

    topic = forms.ChoiceField(choices=NoticeTopic.choices, label=_("Notice topic"))
    text = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 10}),
        strip=False,
        max_length=MAX_TEXT,
        label=_("Notice text"),
    )


class DisclosureForm(forms.Form):
    """A clinician attestation bound to one encounter, never a patient consent."""

    encounter_id = forms.UUIDField(label=_("Encounter"))
    informed = forms.BooleanField(
        required=False, label=_("I informed the patient about AI assistance.")
    )
    refused = forms.BooleanField(
        required=False, label=_("The patient refused AI assistance.")
    )


class ParticipantForm(forms.Form):
    """A non-patient voice notice without collecting or inventing an identity."""

    session_id = forms.UUIDField(label=_("Encounter"))
    participant_kind = forms.ChoiceField(
        choices=ParticipantKind.choices, label=_("Participant kind")
    )
