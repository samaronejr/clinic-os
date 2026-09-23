"""Bounded native forms; record identifiers travel only in POST bodies."""

from __future__ import annotations

from django import forms

from apps.retention.models import RECORD_CLASSES

MAX_TEXT = 255


class PolicyForm(forms.Form):
    """Propose one retention policy version for a fixed record class."""

    record_class = forms.ChoiceField(
        label="Classe de registro",
        choices=[(value, value) for value in RECORD_CLASSES],
    )
    retention_days = forms.IntegerField(
        label="Prazo de retenção (dias)",
        min_value=0,
        required=False,
        help_text="Deixe em branco para retenção por prazo indeterminado.",
    )


class HoldForm(forms.Form):
    """Place one legal hold on a known record class and identifier."""

    record_class = forms.ChoiceField(
        label="Classe de registro",
        choices=[(value, value) for value in RECORD_CLASSES],
    )
    record_id = forms.UUIDField(
        label="Identificador do registro",
        help_text="Cole o identificador exibido na lista do registro.",
    )
    authority = forms.CharField(label="Autoridade", max_length=MAX_TEXT)
    reason = forms.CharField(
        label="Motivo",
        max_length=MAX_TEXT,
        widget=forms.Textarea(attrs={"rows": 2}),
    )


class HoldReleaseForm(forms.Form):
    """Release one active hold with its own authority and reason."""

    hold_id = forms.UUIDField(
        label="Identificador da guarda",
        help_text="Cole o identificador exibido na lista de guardas.",
    )
    release_authority = forms.CharField(label="Autoridade", max_length=MAX_TEXT)
    release_reason = forms.CharField(
        label="Motivo da liberação",
        max_length=MAX_TEXT,
        widget=forms.Textarea(attrs={"rows": 2}),
    )


class ReleaseForm(forms.Form):
    """Release one exact finalized or superseded version to its patient."""

    version_id = forms.UUIDField(widget=forms.HiddenInput)


class ReleaseRevokeForm(forms.Form):
    """Revoke one existing release; the document itself is unchanged."""

    release_id = forms.UUIDField(widget=forms.HiddenInput)


class ExportForm(forms.Form):
    """Export one patient's released records; the patient id stays in the body."""

    patient_id = forms.UUIDField(widget=forms.HiddenInput)


class DisposalForm(forms.Form):
    """Evaluate one record's disposal eligibility; never deletes anything."""

    record_class = forms.ChoiceField(
        label="Classe de registro",
        choices=[(value, value) for value in RECORD_CLASSES],
    )
    record_id = forms.UUIDField(
        label="Identificador do registro",
        help_text="Cole o identificador exibido na lista do registro.",
    )
