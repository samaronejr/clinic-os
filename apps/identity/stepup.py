"""Fail-closed recent-TOTP verification for sensitive identity boundaries."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.identity.models import User

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Concatenate, ParamSpec

    from django.http import HttpRequest, HttpResponseBase

    P = ParamSpec("P")

STEP_UP_SESSION_KEY: Final = "otp_verified_at"
STEP_UP_INTENT_SESSION_KEY: Final = "otp_step_up_intent"
DEFAULT_STEP_UP_MAX_AGE_SECONDS: Final = int(settings.STEP_UP_MAX_AGE_SECONDS)
MAX_INTENT_FACTS: Final = 6
MAX_INTENT_TEXT: Final = 160


class StepUpFailureReason(StrEnum):
    """Stable reason categories for service and HTTP boundaries."""

    RECENT_VERIFICATION_REQUIRED = "recent_verification_required"


@dataclass(frozen=True, slots=True)
class StepUpRequired(PermissionDenied):
    """Signal that a sensitive operation requires recent TOTP verification."""

    reason: StepUpFailureReason = StepUpFailureReason.RECENT_VERIFICATION_REQUIRED

    def __str__(self) -> str:
        """Return the stable non-sensitive permission message."""
        return "recent authenticator verification is required"


def _utc_now_seconds() -> int:
    return int(time.time())


def clear_step_up_verification(
    request: HttpRequest,
    *,
    clear_device: bool = False,
) -> None:
    """Remove invalid freshness and optionally its OTP device binding."""
    request.session.pop(STEP_UP_SESSION_KEY, None)
    if clear_device:
        request.session.pop(DEVICE_ID_SESSION_KEY, None)


def stamp_step_up_verification(request: HttpRequest) -> None:
    """Record the UTC Unix second of a successful exact-device verification."""
    request.session[STEP_UP_SESSION_KEY] = _utc_now_seconds()


def set_step_up_intent(
    request: HttpRequest,
    *,
    target: str,
    action: str,
    facts: list[tuple[str, str]],
) -> None:
    """Describe, for the challenge screen, the exact action being confirmed.

    The intent is display-only context bound to one continuation target: it
    names the action and the fixed subject facts (patient, issuer, clinic)
    so the physician can see what a fresh code will authorize. It carries
    no authority; the service boundary re-derives everything from stored
    records. Sessions are server-side, so the facts never reach a cookie.
    """
    request.session[STEP_UP_INTENT_SESSION_KEY] = {
        "target": target,
        "action": action[:MAX_INTENT_TEXT],
        "facts": [
            [str(label)[:MAX_INTENT_TEXT], str(value)[:MAX_INTENT_TEXT]]
            for label, value in facts[:MAX_INTENT_FACTS]
        ],
    }


def step_up_intent_for(request: HttpRequest, target: str) -> dict[str, object] | None:
    """Return the stored intent only when it was set for this exact target."""
    intent = request.session.get(STEP_UP_INTENT_SESSION_KEY)
    if not isinstance(intent, dict) or intent.get("target") != target:
        return None
    action = intent.get("action")
    facts = intent.get("facts")
    if not isinstance(action, str) or not isinstance(facts, list):
        return None
    return {"action": action, "facts": facts}


def clear_step_up_intent(request: HttpRequest) -> None:
    """Drop the display intent once the challenge completes or is abandoned."""
    request.session.pop(STEP_UP_INTENT_SESSION_KEY, None)


def _freshness_is_valid(request: HttpRequest, max_age: int) -> bool:
    user = request.user
    is_verified = getattr(user, "is_verified", None)
    if (
        not isinstance(user, User)
        or not user.is_authenticated
        or not user.is_active
        or not callable(is_verified)
        or not bool(is_verified())
    ):
        clear_step_up_verification(request, clear_device=True)
        return False

    device = getattr(user, "otp_device", None)
    persistent_id = request.session.get(DEVICE_ID_SESSION_KEY)
    if (
        not isinstance(device, TOTPDevice)
        or not device.confirmed
        or device.user_id != user.pk
        or not isinstance(persistent_id, str)
        or persistent_id != device.persistent_id
    ):
        clear_step_up_verification(request, clear_device=True)
        return False

    verified_at = request.session.get(STEP_UP_SESSION_KEY)
    if isinstance(verified_at, bool) or not isinstance(verified_at, int):
        clear_step_up_verification(request)
        return False
    age = _utc_now_seconds() - verified_at
    if age < 0 or age > max_age:
        clear_step_up_verification(request)
        return False
    return True


def assert_step_up(
    request: HttpRequest,
    *,
    max_age: int = DEFAULT_STEP_UP_MAX_AGE_SECONDS,
) -> None:
    """Raise a typed permission error unless recent TOTP is still valid."""
    if isinstance(max_age, bool) or not isinstance(max_age, int) or max_age < 0:
        clear_step_up_verification(request)
        raise StepUpRequired
    if not _freshness_is_valid(request, max_age):
        raise StepUpRequired


def require_recent_verification(
    max_age: int = DEFAULT_STEP_UP_MAX_AGE_SECONDS,
) -> Callable[
    [Callable[Concatenate[HttpRequest, P], HttpResponseBase]],
    Callable[Concatenate[HttpRequest, P], HttpResponseBase],
]:
    """Translate a stale service boundary into the named step-up challenge."""

    def decorator(
        view: Callable[Concatenate[HttpRequest, P], HttpResponseBase],
    ) -> Callable[Concatenate[HttpRequest, P], HttpResponseBase]:
        def wrapped(
            request: HttpRequest,
            /,
            *args: P.args,
            **kwargs: P.kwargs,
        ) -> HttpResponseBase:
            try:
                assert_step_up(request, max_age=max_age)
            except StepUpRequired:
                from apps.identity.otp import (  # noqa: PLC0415
                    flow_redirect,
                    safe_next_url,
                )

                target = safe_next_url(request, request.get_full_path())
                return flow_redirect(request, "identity:step-up", target)
            return view(request, *args, **kwargs)

        return wrapped

    return decorator
