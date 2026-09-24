"""Bounded native upload input; file bytes never enter URLs or storage keys."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django import forms

from apps.ehr.attachments import MAX_ATTACHMENT_BYTES

if TYPE_CHECKING:
    from django.core.files.uploadedfile import UploadedFile


class AttachmentUploadForm(forms.Form):
    """Bind the encounter identity and the file to this tab's submitted body."""

    encounter_id = forms.UUIDField(widget=forms.HiddenInput)
    attachment = forms.FileField(
        label="Arquivo clínico",
        help_text=(
            "PDF, JPEG ou PNG de até 10 MiB. O arquivo fica em verificação "
            "antes de ficar disponível."
        ),
    )

    def clean_attachment(self) -> UploadedFile:
        """Reject oversized uploads before the service reads the bytes."""
        upload: UploadedFile = self.cleaned_data["attachment"]
        if upload.size is not None and upload.size > MAX_ATTACHMENT_BYTES:
            message = "O arquivo excede o limite de 10 MiB."
            raise forms.ValidationError(message)
        return upload
