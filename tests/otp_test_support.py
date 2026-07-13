from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Final
from unittest.mock import patch
from uuid import UUID, uuid4

from apps.identity.models import User, UserClinicRole
from django.contrib.auth.hashers import make_password
from django.db import connection, transaction
from django_otp.oath import TOTP

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.http import HttpResponseBase
    from django.test import Client
    from django_otp.plugins.otp_totp.models import TOTPDevice

    from rbac_fixtures import RbacGraph

OTP_FIXED_TIME: Final = 2_000_000_000.0
OTP_RAW_CREDENTIAL: Final = "todo9-correct-horse-battery-staple"


@contextmanager
def runtime_role() -> Iterator[None]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        row = cursor.fetchone()
        already_runtime = row == ("clinic_app",)
        if not already_runtime:
            cursor.execute("SET ROLE clinic_app")
    try:
        yield
    finally:
        if not already_runtime:
            with connection.cursor() as cursor:
                cursor.execute("RESET ROLE")


@contextmanager
def fixed_otp_time() -> Iterator[None]:
    with patch(
        "django_otp.plugins.otp_totp.models.time.time",
        return_value=OTP_FIXED_TIME,
    ):
        yield


@contextmanager
def current_user_guc(user_id: UUID) -> Iterator[None]:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_user_id', %s, true)",
            [str(user_id)],
        )
        yield


def token_for(device: TOTPDevice) -> str:
    generator = TOTP(
        device.bin_key,
        device.step,
        device.t0,
        device.digits,
        device.drift,
    )
    generator.time = OTP_FIXED_TIME
    return f"{generator.token():0{device.digits}d}"


def create_totp_device(
    user_id: UUID,
    *,
    confirmed: bool,
) -> TOTPDevice:
    from django_otp.plugins.otp_totp.models import TOTPDevice  # noqa: PLC0415

    with runtime_role(), current_user_guc(user_id):
        return TOTPDevice.objects.create(
            user_id=user_id,
            name="Clinic OS authenticator",
            confirmed=confirmed,
        )


def get_totp_device(
    user_id: UUID,
    *,
    confirmed: bool,
) -> TOTPDevice:
    from django_otp.plugins.otp_totp.models import TOTPDevice  # noqa: PLC0415

    with runtime_role(), current_user_guc(user_id):
        return TOTPDevice.objects.get(user_id=user_id, confirmed=confirmed)


def totp_device_exists(user_id: UUID, *, confirmed: bool) -> bool:
    from django_otp.plugins.otp_totp.models import TOTPDevice  # noqa: PLC0415

    with runtime_role(), current_user_guc(user_id):
        return bool(
            TOTPDevice.objects.filter(
                user_id=user_id,
                confirmed=confirmed,
            ).exists()
        )


def create_receptionist(graph: RbacGraph) -> User:
    user = User.objects.create(
        username=f"todo9-receptionist-{uuid4().hex}",
        password=make_password(OTP_RAW_CREDENTIAL),
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(graph.organization_a)],
        )
        UserClinicRole.objects.create(
            user_id=user.pk,
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            role=UserClinicRole.Role.RECEPTIONIST,
        )
    return user


def login(
    client: Client,
    username: str,
    *,
    next_url: str = "/auth/protected/",
    password: str = OTP_RAW_CREDENTIAL,
) -> HttpResponseBase:
    return client.post(
        "/auth/login/",
        {
            "username": username,
            "password": password,
            "next": next_url,
        },
    )
