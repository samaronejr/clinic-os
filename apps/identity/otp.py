"""User-scoped TOTP enrollment, verification, and enforcement primitives."""

from __future__ import annotations

import base64
from functools import wraps
from importlib import import_module
from io import BytesIO
from typing import TYPE_CHECKING, Concatenate, Final, Protocol, TypedDict, cast
from urllib.parse import quote, urlencode

from django.conf import settings
from django.contrib.auth import logout as session_logout
from django.db import connection, transaction
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.cache import patch_cache_control, patch_vary_headers
from django.views.decorators.debug import sensitive_variables
from django_otp import login as otp_login
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.identity import redirects as _redirects
from apps.identity.models import User
from apps.identity.stepup import stamp_step_up_verification

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from uuid import UUID

    from django_otp.models import VerifyNotAllowed


class OTPVerificationDetails(TypedDict, total=False):
    """Describe optional throttle details returned by django-otp."""

    error_message: str
    reason: VerifyNotAllowed
    failure_count: int
    locked_until: datetime


class TotpDevice(Protocol):
    """Describe the typed django-otp device surface used by Clinic OS."""

    pk: int
    user_id: UUID
    name: str
    confirmed: bool
    persistent_id: str
    bin_key: bytes
    digits: int
    step: int

    def verify_is_allowed(
        self,
    ) -> tuple[bool, OTPVerificationDetails | None]:
        """Return the django-otp throttle decision and structured details."""

    def verify_token(self, token: str) -> bool:
        """Verify and consume one time-based code."""

    def save(self, *, update_fields: tuple[str, ...]) -> None:
        """Persist selected device fields."""


class _QrImage(Protocol):
    def save(self, stream: BytesIO, image_format: str, /) -> None: ...


class _QrCode(Protocol):
    def add_data(self, data: str) -> None: ...

    def make(self, *, fit: bool) -> None: ...

    def make_image(self, *, fill_color: str, back_color: str) -> _QrImage: ...


class _QrFactory(Protocol):
    def __call__(self, *, box_size: int, border: int) -> _QrCode: ...


class _QrModule(Protocol):
    QRCode: _QrFactory


QRCODE = cast("_QrModule", import_module("qrcode"))
safe_next_url = _redirects.safe_next_url
SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})

type PrivilegedView[**P] = Callable[Concatenate[HttpRequest, P], HttpResponseBase]
type SafeContinuation[**P] = Callable[P, str]


def confirmed_devices(
    user_id: UUID,
    *,
    for_update: bool = False,
) -> tuple[TotpDevice, ...]:
    """Return confirmed TOTP devices explicitly scoped to one user id."""
    devices = TOTPDevice.objects.filter(user_id=user_id, confirmed=True).order_by("pk")
    if for_update:
        devices = devices.select_for_update()
    return cast("tuple[TotpDevice, ...]", tuple(devices))


def pending_devices(
    user_id: UUID,
    *,
    for_update: bool = False,
) -> tuple[TotpDevice, ...]:
    """Return pending TOTP devices explicitly scoped to one user id."""
    devices = TOTPDevice.objects.filter(user_id=user_id, confirmed=False).order_by("pk")
    if for_update:
        devices = devices.select_for_update()
    return cast("tuple[TotpDevice, ...]", tuple(devices))


def get_or_create_pending_device(user_id: UUID) -> TotpDevice:
    """Serialize enrollment starts so one user receives one pending device."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.pg_advisory_xact_lock("
            "pg_catalog.hashtextextended(%s, 0))",
            [f"clinic-totp:{user_id}"],
        )
        existing = pending_devices(user_id, for_update=True)
        if existing:
            return existing[0]
        return cast(
            "TotpDevice",
            TOTPDevice.objects.create(
                user_id=user_id,
                name="Clinic OS authenticator",
                confirmed=False,
            ),
        )


@sensitive_variables()
def provisioning_qr_data_uri(
    device: TotpDevice,
    username: str,
) -> str:
    """Build an in-memory QR image without dereferencing the device user."""
    issuer = str(settings.OTP_TOTP_ISSUER).replace(":", "")
    secret = base64.b32encode(device.bin_key).decode("ascii")
    label = quote(f"{issuer}:{username}", safe="")
    parameters = urlencode(
        {
            "secret": secret,
            "algorithm": "SHA1",
            "digits": device.digits,
            "period": device.step,
            "issuer": issuer,
        }
    )
    qr = QRCODE.QRCode(box_size=6, border=4)
    qr.add_data(f"otpauth://totp/{label}?{parameters}")
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    image.save(buffer, "PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def auth_response(response: HttpResponseBase) -> HttpResponseBase:
    """Mark authentication responses private and HTMX-variant aware."""
    patch_cache_control(
        response,
        no_cache=True,
        no_store=True,
        must_revalidate=True,
        private=True,
    )
    patch_vary_headers(response, ("HX-Request",))
    return response


def auth_redirect(request: HttpRequest, target: str) -> HttpResponseBase:
    """Redirect progressively, using HX-Redirect only for HTMX requests."""
    if request.headers.get("HX-Request") == "true":
        response: HttpResponseBase = HttpResponse(status=204)
        response.headers["HX-Redirect"] = target
    else:
        response = redirect(target)
    return auth_response(response)


def flow_redirect(
    request: HttpRequest,
    route_name: str,
    target: str,
) -> HttpResponseBase:
    """Send the current safe destination through an authentication step."""
    flow_url = reverse(route_name)
    query = urlencode({"next": target})
    return auth_redirect(request, f"{flow_url}?{query}")


def is_privileged_user(user: User) -> bool:
    """Use canonical tenant role assignments to identify 2FA-required users."""
    return user.is_physician_anywhere or user.is_clinic_admin_anywhere


def is_confirmed_verified_user(user: User) -> bool:
    """Reject forged, pending, or foreign session device identifiers."""
    device = getattr(user, "otp_device", None)
    return bool(
        isinstance(device, TOTPDevice)
        and device.confirmed
        and device.user_id == user.pk
    )


def record_otp_verification(request: HttpRequest, device: TotpDevice) -> None:
    """Rotate the password session before binding a confirmed user device."""
    if not device.confirmed or device.user_id != request.user.pk:
        msg = "a confirmed TOTP device for the current user is required"
        raise ValueError(msg)
    request.session.cycle_key()
    otp_login(request, device)
    stamp_step_up_verification(request)


def privileged_totp_required[**P](
    safe_continuation: SafeContinuation[P],
) -> Callable[[PrivilegedView[P]], PrivilegedView[P]]:
    """Guard privileged roles and resume unsafe methods at a declared safe GET."""

    def decorate(view: PrivilegedView[P]) -> PrivilegedView[P]:
        @wraps(view)
        def wrapped(
            request: HttpRequest,
            /,
            *args: P.args,
            **kwargs: P.kwargs,
        ) -> HttpResponseBase:
            target = (
                safe_next_url(request, request.get_full_path())
                if request.method in SAFE_METHODS
                else safe_next_url(request, safe_continuation(*args, **kwargs))
            )
            user = request.user
            if not user.is_authenticated:
                session_logout(request)
                return flow_redirect(request, "identity:login", target)
            if not isinstance(user, User) or not is_privileged_user(user):
                return view(request, *args, **kwargs)
            if is_confirmed_verified_user(user):
                return view(request, *args, **kwargs)
            route = (
                "identity:verify" if confirmed_devices(user.pk) else "identity:enroll"
            )
            return flow_redirect(request, route, target)

        return wrapped

    return decorate
