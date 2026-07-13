"""User-scoped TOTP enrollment, verification, and enforcement primitives."""

from __future__ import annotations

import base64
import posixpath
from functools import wraps
from importlib import import_module
from io import BytesIO
from typing import TYPE_CHECKING, Final, Protocol, TypedDict, cast
from urllib.parse import quote, unquote, urlencode, urlsplit

from django.conf import settings
from django.contrib.auth import logout as session_logout
from django.db import connection, transaction
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.cache import patch_cache_control, patch_vary_headers
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.debug import sensitive_variables
from django_otp import login as otp_login
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.identity.models import User

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
AUTH_FLOW_PATHS: Final = frozenset(
    {
        "/auth/login",
        "/auth/enroll",
        "/auth/verify",
        "/auth/logout",
    }
)
DEFAULT_AUTH_TARGET: Final = "/auth/protected/"
ASCII_CONTROL_LIMIT: Final = 0x20
ASCII_DELETE: Final = 0x7F


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


def _canonical_target(raw_target: str) -> tuple[str, str] | None:
    decoded_target = raw_target
    for _attempt in range(4):
        try:
            next_target = unquote(decoded_target, errors="strict")
        except UnicodeDecodeError:
            return None
        if next_target == decoded_target:
            break
        decoded_target = next_target
    else:
        return None
    if "\\" in decoded_target or any(
        ord(character) < ASCII_CONTROL_LIMIT or ord(character) == ASCII_DELETE
        for character in decoded_target
    ):
        return None
    path = urlsplit(decoded_target).path
    if not path.startswith("/"):
        return None
    canonical_path = posixpath.normpath(f"/{path.lstrip('/')}")
    return decoded_target, canonical_path.rstrip("/") or "/"


def safe_next_url(request: HttpRequest, raw_target: str | None) -> str:
    """Accept only a same-host non-auth-flow redirect target."""
    if not raw_target:
        return DEFAULT_AUTH_TARGET
    canonical_target = _canonical_target(raw_target)
    if canonical_target is None:
        return DEFAULT_AUTH_TARGET
    decoded_target, canonical_path = canonical_target
    allowed_hosts = {request.get_host()}
    require_https = request.is_secure()
    if not all(
        url_has_allowed_host_and_scheme(
            target,
            allowed_hosts=allowed_hosts,
            require_https=require_https,
        )
        for target in (raw_target, decoded_target)
    ):
        return DEFAULT_AUTH_TARGET
    if canonical_path in AUTH_FLOW_PATHS:
        return DEFAULT_AUTH_TARGET
    return raw_target


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


def privileged_totp_required(
    view: Callable[[HttpRequest], HttpResponseBase],
) -> Callable[[HttpRequest], HttpResponseBase]:
    """Require confirmed TOTP for privileged roles and leave other roles unchanged."""

    @wraps(view)
    def wrapped(request: HttpRequest) -> HttpResponseBase:
        user = request.user
        if not user.is_authenticated:
            target = safe_next_url(request, request.get_full_path())
            session_logout(request)
            return flow_redirect(
                request,
                "identity:login",
                target,
            )
        if not isinstance(user, User) or not is_privileged_user(user):
            return view(request)
        if is_confirmed_verified_user(user):
            return view(request)
        target = safe_next_url(request, request.get_full_path())
        route = "identity:verify" if confirmed_devices(user.pk) else "identity:enroll"
        return flow_redirect(request, route, target)

    return wrapped
