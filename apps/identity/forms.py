"""Authentication forms with explicit, non-replayable OTP device selection."""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final, cast

from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.db import transaction
from django.http import HttpRequest, QueryDict
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.identity.models import User
from apps.identity.otp import TotpDevice

if TYPE_CHECKING:
    from uuid import UUID

INVALID_LOGIN_MESSAGE: Final = "Check your username and password, then try again."
INVALID_CODE_MESSAGE: Final = "That code is not valid. Try a current code."


class _NonEchoingChoiceField(forms.ChoiceField):
    def bound_data(self, data: str | None, initial: str | None) -> str | None:
        return data if self.valid_value(data) else initial


class ClinicAuthenticationForm(AuthenticationForm):
    """Render password authentication without disclosing account state."""

    def __init__(
        self,
        request: HttpRequest | None = None,
        data: QueryDict | None = None,
    ) -> None:
        """Apply accessible attributes while preserving Django validation."""
        super().__init__(request=request, data=data)
        self.error_messages = {
            "invalid_login": INVALID_LOGIN_MESSAGE,
            "inactive": INVALID_LOGIN_MESSAGE,
        }
        self.fields["username"].widget.attrs.update(
            {
                "autocomplete": "username",
                "autocapitalize": "none",
                "spellcheck": "false",
                "aria-describedby": "login-help",
            }
        )
        self.fields["password"].widget.attrs.update(
            {
                "autocomplete": "current-password",
                "aria-describedby": "login-help",
            }
        )


class ExplicitOTPTokenForm(forms.Form):
    """Verify only a caller-supplied, persistent TOTP device choice."""

    otp_device = _NonEchoingChoiceField(
        choices=(),
        error_messages={
            "required": INVALID_CODE_MESSAGE,
            "invalid_choice": INVALID_CODE_MESSAGE,
        },
    )
    otp_token = forms.CharField(
        label="Authentication code",
        widget=forms.PasswordInput(render_value=False),
    )

    def __init__(
        self,
        user: User,
        devices: Sequence[TotpDevice],
        request: HttpRequest | None = None,
        data: QueryDict | None = None,
    ) -> None:
        """Bind validation choices to the exact devices loaded for this user."""
        del request
        self._user_id: UUID = user.pk
        self._allowed_devices = {
            device.persistent_id: (device.pk, device.confirmed) for device in devices
        }
        self._selected_device: TotpDevice | None = None
        super().__init__(data=data)
        choices = [(device.persistent_id, device.name) for device in devices]
        device_field = cast("forms.ChoiceField", self.fields["otp_device"])
        device_field.choices = choices
        if len(choices) == 1:
            device_field.widget = forms.HiddenInput()
            self.initial["otp_device"] = choices[0][0]
        else:
            device_field.widget = forms.Select(choices=choices)
        self.fields["otp_token"].widget = forms.PasswordInput(
            render_value=False,
            attrs={
                "autocomplete": "one-time-code",
                "inputmode": "numeric",
                "pattern": "[0-9]*",
                "aria-describedby": "otp-help",
            },
        )

    def clean_otp_token(self) -> str:
        """Lock and verify the token against the selected user-owned device."""
        device_id = self.cleaned_data.get("otp_device")
        token = self.cleaned_data.get("otp_token")
        if not isinstance(token, str):
            raise forms.ValidationError(INVALID_CODE_MESSAGE, code="invalid_token")
        if not isinstance(device_id, str):
            return token
        expected = self._allowed_devices.get(device_id)
        if expected is None:
            raise forms.ValidationError(INVALID_CODE_MESSAGE, code="invalid_token")
        device_pk, confirmed = expected
        validation_error: forms.ValidationError | None = None
        with transaction.atomic():
            loaded = (
                TOTPDevice.objects.select_for_update()
                .filter(
                    pk=device_pk,
                    user_id=self._user_id,
                    confirmed=confirmed,
                )
                .first()
            )
            device = cast("TotpDevice | None", loaded)
            if device is None:
                validation_error = forms.ValidationError(
                    INVALID_CODE_MESSAGE,
                    code="invalid_token",
                )
            else:
                allowed, details = device.verify_is_allowed()
                if not allowed:
                    message = (
                        details.get("error_message") if details is not None else None
                    )
                    if not isinstance(message, str):
                        message = "Verification temporarily disabled. Try again soon."
                    validation_error = forms.ValidationError(
                        message,
                        code="verification_not_allowed",
                    )
                elif not device.verify_token(token):
                    validation_error = forms.ValidationError(
                        INVALID_CODE_MESSAGE,
                        code="invalid_token",
                    )
                else:
                    self._selected_device = device
        if validation_error is not None:
            raise validation_error
        return token

    def selected_device(self) -> TotpDevice | None:
        """Return the verified device only when it was in the bound choices."""
        return self._selected_device
