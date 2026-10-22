"""Body-only selectors and protected native staff lifecycle inputs."""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.identity.models import UserClinicRole


class StaffInvitationForm(forms.Form):
    """Invite one email into a canonical current role bundle."""

    email = forms.EmailField(label=_("Staff email"))
    bundle = forms.ChoiceField(
        label=_("Role bundle"),
        choices=UserClinicRole.Role.choices,
        initial=UserClinicRole.Role.RECEPTIONIST,
    )


class OffboardingForm(forms.Form):
    """Keep the target out of URLs, including refusal and return destinations."""

    user_id = forms.UUIDField(widget=forms.HiddenInput)
    confirm = forms.BooleanField(required=True)


class InvitationRevocationForm(forms.Form):
    """Keep even pending account selectors in the protected request body."""

    invitation_id = forms.UUIDField(widget=forms.HiddenInput)


class AuthenticatorResetForm(forms.Form):
    """Select one scoped account for a fresh administrator reset request."""

    user_id = forms.UUIDField(widget=forms.HiddenInput)


class AuthenticatorResetExecutionForm(forms.Form):
    """Select one notification-clocked request through its private POST body."""

    reset_id = forms.UUIDField(widget=forms.HiddenInput)


class InvitationEnrollmentForm(forms.Form):
    """Never redisplay invitation tokens, passwords or TOTP input."""

    invitation_code = forms.RegexField(
        regex=r"^[0-9a-f]{64}$",
        label=_("Private invitation code"),
        widget=forms.PasswordInput(attrs={"autocomplete": "off"}),
    )
    username = forms.CharField(label=_("Username"), max_length=150)
    password = forms.CharField(
        label=_("Password"),
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password_confirmation = forms.CharField(
        label=_("Confirm password"),
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    otp_token = forms.RegexField(
        regex=r"^[0-9]{6}$",
        label=_("Authenticator code"),
        required=False,
        widget=forms.PasswordInput(
            attrs={"autocomplete": "one-time-code", "inputmode": "numeric"}
        ),
    )

    def clean_password_confirmation(self) -> str:
        """Compare only in memory; neither value enters enrollment storage."""
        password: str = self.cleaned_data["password_confirmation"]
        if password != self.cleaned_data.get("password"):
            message = _("The passwords do not match.")
            raise forms.ValidationError(message, code="password_mismatch")
        return password
