"""Private body-only account recovery and session controls."""

from django import forms
from django.utils.translation import gettext_lazy as _


class RecoveryCodeForm(forms.Form):
    """Do not echo a secret after success or failed validation."""

    code = forms.RegexField(
        regex=r"^[0-9a-fA-F]{32}$",
        label=_("Recovery code"),
        widget=forms.PasswordInput(attrs={"autocomplete": "off"}),
    )


class SessionRevocationForm(forms.Form):
    """Revoke a user-owned opaque inventory record, not a raw framework key."""

    session_id = forms.UUIDField(widget=forms.HiddenInput)
