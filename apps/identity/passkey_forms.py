"""Native body-only own credential and organization policy controls."""

from django import forms
from django.utils.translation import gettext_lazy as _


class PasskeyRegistrationForm(forms.Form):
    """An optional protected nickname is never an identity or authority claim."""

    nickname = forms.CharField(
        required=False,
        max_length=80,
        label=_("Device nickname"),
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )


class PasskeyRemovalForm(forms.Form):
    """Keep both scope and the credential selector out of URLs."""

    clinic_id = forms.UUIDField(widget=forms.HiddenInput)
    passkey_id = forms.UUIDField(widget=forms.HiddenInput)


class PasskeyPolicyForm(forms.Form):
    """Explicit opt-in, bounded to the native checkbox's actual value."""

    clinic_id = forms.UUIDField(widget=forms.HiddenInput)
    enabled = forms.BooleanField(
        required=False,
        label=_("Allow passkeys for privileged sign-in and sensitive actions"),
    )

    def clean_enabled(self) -> bool:
        """Reject arbitrary strings instead of treating them as a true checkbox."""
        if self.data.get("enabled") not in {None, "", "on"}:
            raise forms.ValidationError(
                _("Choose whether passkey verification is allowed."),
                code="invalid_policy",
            )
        value: bool = self.cleaned_data["enabled"]
        return value
