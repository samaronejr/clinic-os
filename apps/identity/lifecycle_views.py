"""Native, no-store HTTP boundaries for the staff identity lifecycle."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.identity.lifecycle_forms import SignupForm
from apps.identity.otp import auth_response
from apps.identity.signup import SignupDeniedError, submit_signup

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest, HttpResponseBase


@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def signup_view(request: HttpRequest) -> HttpResponseBase:
    """Receive a pending request; neither authenticate nor issue clinic authority."""
    form = SignupForm(request.POST if request.method == "POST" else None)
    pending = False
    if request.method == "POST" and form.is_valid():
        try:
            submit_signup(
                capture=form.capture(),
                idempotency_key=cast("UUID", form.cleaned_data["idempotency_key"]),
            )
        except SignupDeniedError:
            form.add_error(
                None,
                _(
                    "We could not register this request. "
                    "Check the details and try again."
                ),
            )
        else:
            pending = True
    response = render(
        request,
        "identity/signup.html",
        {"form": form, "pending": pending},
        status=400 if form.errors else 200,
    )
    return auth_response(response)
