"""Bounded native forms for explicit non-medication clinical documents."""

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.orders.values import CATALOG
from apps.prescription.documents import (
    MAX_DOCUMENT_BODY,
    MAX_RECIPIENT,
    DocumentContentInput,
)


class DocumentActionForm(forms.Form):
    """Actions compare the saved draft version, never an untrusted preview."""

    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class DocumentContentForm(DocumentActionForm):
    """Keep bound text verbatim after validation failures and stale saves."""

    body = forms.CharField(
        label=_("Document text"),
        max_length=MAX_DOCUMENT_BODY,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 10}),
    )
    recipient = forms.CharField(
        label=_("Recipient (optional)"),
        max_length=MAX_RECIPIENT,
        required=False,
        strip=False,
    )
    exam_codes = forms.MultipleChoiceField(
        label=_("Requested examinations"),
        choices=tuple(CATALOG.items()),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    cid10_code = forms.CharField(
        label=_("CID-10 (optional)"),
        required=False,
        disabled=True,
        help_text=_("Unavailable: no versioned terminology source has passed EG-13."),
    )
    cid10_source_version = forms.CharField(
        label=_("CID-10 source version"),
        required=False,
        disabled=True,
    )

    def clean_cid10_code(self) -> str:
        """Refuse forged diagnosis input even though the field is disabled."""
        if self.data.get("cid10_code"):
            raise ValidationError(
                _("CID-10 is unavailable until a versioned source passes EG-13.")
            )
        return ""

    def clean_cid10_source_version(self) -> str:
        """Refuse caller-provided versions without clinical-content approval."""
        if self.data.get("cid10_source_version"):
            raise ValidationError(
                _("CID-10 is unavailable until a versioned source passes EG-13.")
            )
        return ""

    def content(self) -> DocumentContentInput:
        """Construct the service value only after successful form validation."""
        return DocumentContentInput(
            body=self.cleaned_data["body"],
            recipient=self.cleaned_data["recipient"],
            exam_codes=tuple(self.cleaned_data["exam_codes"]),
        )
