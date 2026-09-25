"""Display preferences screen: theme and density, native form, server-side."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django import forms
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from apps.identity.models import UserPreference
from apps.identity.otp import privileged_totp_required
from apps.identity.preferences import load_ui_preferences, save_ui_preferences

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.http import HttpRequest, HttpResponseBase

BAD_REQUEST = 400


class UiPreferencesForm(forms.Form):
    """Closed choices; anything else is a validation error, never stored."""

    theme = forms.ChoiceField(
        choices=UserPreference.Theme.choices,
        error_messages={"invalid_choice": _("Choose light or dark.")},
    )
    density = forms.ChoiceField(
        choices=UserPreference.Density.choices,
        error_messages={"invalid_choice": _("Choose comfortable or compact.")},
    )


def preferences_continuation() -> str:
    """Resume an interrupted POST at the preferences page."""
    return reverse("identity:preferences")


def _options(choices: Iterable[tuple[str, object]]) -> list[dict[str, str]]:
    return [{"value": value, "label": str(label)} for value, label in choices]


@require_http_methods(["GET", "POST"])
@privileged_totp_required(preferences_continuation)
def preferences_view(request: HttpRequest) -> HttpResponseBase:
    """Show and save the current user's own theme and density."""
    current = load_ui_preferences()
    saved = False
    status = 200
    if request.method == "POST":
        form = UiPreferencesForm(request.POST)
        if form.is_valid():
            current = save_ui_preferences(
                theme=form.cleaned_data["theme"],
                density=form.cleaned_data["density"],
            )
            saved = True
        else:
            status = BAD_REQUEST
    else:
        form = UiPreferencesForm(
            initial={"theme": current.theme, "density": current.density}
        )
    response = render(
        request,
        "identity/preferences.html",
        {
            "form": form,
            "current": current,
            "saved": saved,
            "theme_options": _options(UserPreference.Theme.choices),
            "density_options": _options(UserPreference.Density.choices),
        },
        status=status,
    )
    response["Cache-Control"] = "no-store, private"
    return response
