"""Accessible native forms for signup and staff identity lifecycle actions."""

from __future__ import annotations

from typing import cast
from uuid import uuid4

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.identity.cnpj import validate_cnpj
from apps.identity.signup import SignupCapture


class LifecycleForm(forms.Form):
    """Wire the shared field primitive's hints and errors to its controls."""

    def full_clean(self) -> None:
        """Keep hidden secrets out of errors and visible descriptions linked."""
        super().full_clean()
        for name, field in self.fields.items():
            if field.widget.is_hidden:
                continue
            descriptions: list[str] = []
            if field.help_text:
                descriptions.append(f"{self[name].auto_id}_hint")
            if name in self.errors:
                descriptions.append(f"{self[name].auto_id}_error")
            if descriptions:
                field.widget.attrs["aria-describedby"] = " ".join(descriptions)
            if self.errors:
                field.widget.attrs.pop("autofocus", None)


class SignupForm(LifecycleForm):
    """Capture the owner's request without activating a login or subscription."""

    organization_name = forms.CharField(
        max_length=255,
        label=_("Organization name"),
        widget=forms.TextInput(attrs={"autocomplete": "organization"}),
    )
    cnpj = forms.CharField(
        max_length=18,
        label=_("CNPJ"),
        help_text=_("Enter the organization's CNPJ, with or without punctuation."),
    )
    legal_responsible = forms.CharField(
        max_length=255,
        label=_("Legal responsible"),
        widget=forms.TextInput(attrs={"autocomplete": "name"}),
    )
    username = forms.CharField(
        max_length=150,
        label=_("First administrator username"),
        widget=forms.TextInput(
            attrs={
                "autocomplete": "username",
                "autocapitalize": "none",
                "spellcheck": "false",
            }
        ),
    )
    email = forms.EmailField(
        max_length=254,
        label=_("First administrator email"),
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
    )
    professional_registration = forms.CharField(
        required=False,
        max_length=512,
        label=_("Professional registration"),
        help_text=_(
            "If applicable, enter the council, registration number and state. "
            "This does not verify a credential."
        ),
    )
    password = forms.CharField(
        label=_("Password"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password_confirm = forms.CharField(
        label=_("Confirm password"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    idempotency_key = forms.UUIDField(initial=uuid4, widget=forms.HiddenInput())

    def clean_cnpj(self) -> str:
        """Use the retained normalizer as a typed field hook, not a None validator."""
        return validate_cnpj(cast("str", self.cleaned_data["cnpj"]))

    def clean_password_confirm(self) -> str:
        """Require the same password without returning it in any error."""
        confirmation = cast("str", self.cleaned_data["password_confirm"])
        if confirmation != self.cleaned_data.get("password"):
            raise forms.ValidationError(
                _("The passwords do not match. Enter them again."),
                code="password_mismatch",
            )
        return confirmation

    def capture(self) -> SignupCapture:
        """Return the typed values only after Django has accepted the form."""
        return SignupCapture(
            organization_name=cast("str", self.cleaned_data["organization_name"]),
            cnpj=cast("str", self.cleaned_data["cnpj"]),
            legal_responsible=cast("str", self.cleaned_data["legal_responsible"]),
            username=cast("str", self.cleaned_data["username"]),
            email=cast("str", self.cleaned_data["email"]),
            professional_registration=cast(
                "str", self.cleaned_data["professional_registration"]
            ),
            password=cast("str", self.cleaned_data["password"]),
        )
