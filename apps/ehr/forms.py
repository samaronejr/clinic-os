"""Bounded native forms; note text never enters URLs or browser storage."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django import forms

from apps.ehr.services import MAX_CONTENT, SOAP_FIELDS

if TYPE_CHECKING:
    from django.http import QueryDict

    from apps.ehr.models import ClinicalDocumentVersion


class DraftForm(forms.Form):
    """Preserve the submitted revision and edits on every failed save."""

    version_id = forms.UUIDField(widget=forms.HiddenInput)
    revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)

    def __init__(
        self, version: ClinicalDocumentVersion, data: QueryDict | None = None
    ) -> None:
        """Use the exact retained template's prompts, not its newest replacement."""
        super().__init__(
            data=data,
            initial={
                "version_id": version.pk,
                "revision": version.revision,
                **{field: getattr(version, field) for field in SOAP_FIELDS},
            },
        )
        labels = ("Subjetivo", "Objetivo", "Avaliação", "Plano")
        for key, label in zip(SOAP_FIELDS, labels, strict=True):
            self.fields[key] = forms.CharField(
                label=label,
                required=False,
                strip=False,
                max_length=MAX_CONTENT,
                widget=forms.Textarea(attrs={"rows": 4}),
                help_text=version.template.prompts.get(key, ""),
            )

    def content(self) -> dict[str, str]:
        """Return only the four SOAP fields after validation."""
        return {field: self.cleaned_data[field] for field in SOAP_FIELDS}


class AmendmentForm(forms.Form):
    """Bind the amendment base and its required reason to this POST body."""

    version_id = forms.UUIDField(widget=forms.HiddenInput)
    reason = forms.CharField(
        label="Motivo da retificação",
        max_length=255,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
