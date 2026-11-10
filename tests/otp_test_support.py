from __future__ import annotations

import os
import secrets
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final
from unittest.mock import patch
from uuid import UUID, uuid4

import psycopg
from apps.identity.models import User, UserClinicRole
from django.contrib.auth.hashers import make_password
from django.db import connection, transaction
from django_otp.oath import TOTP

from database_urls import database_url_for_name

# Device secrets created in-process, so a committed read is never required.
_TEST_TOTP_SECRETS: dict[int, bytes] = {}

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


def totp_generator(device: TOTPDevice, *, secret_hex: str | None = None) -> TOTP:
    """Build a django-otp generator from the relocated secret at the DB clock.

    The SQL verifier reads ``statement_timestamp()``, so both the code and its
    counter come from the database clock, not a Python clock: ``fixed_otp_time``
    patches the shared ``time`` module globally, which would otherwise skew
    generation away from the verifier's real time. The epoch and any secret read
    run on one dedicated connection that is always closed and never touches the
    Django connection (which some callers fork a management command from).
    ``secret_hex`` is the provisioning secret an enrolment returned for its QR,
    what a real user scans; a device enrolled inside a still-open transaction
    needs it because the dedicated connection cannot see that secret row.
    """
    secret = (
        bytes.fromhex(secret_hex)
        if secret_hex is not None
        else _TEST_TOTP_SECRETS.get(device.pk)
    )
    url = database_url_for_name(
        os.environ["TEST_SUPERUSER_DATABASE_URL"],
        str(connection.settings_dict["NAME"]),
    )
    external = psycopg.connect(url, autocommit=True)
    try:
        with external.cursor() as cursor:
            cursor.execute("SELECT extract(epoch FROM statement_timestamp())")
            clock = cursor.fetchone()
            assert clock is not None
            now_epoch = float(clock[0])
            if secret is None:
                cursor.execute(
                    "SELECT secret FROM clinic_app.identity_totpsecret "
                    "WHERE device_id = %s",
                    [device.pk],
                )
                row = cursor.fetchone()
                secret = (
                    bytes(row[0]) if row is not None and row[0] is not None else b""
                )
    finally:
        external.close()
    generator = TOTP(secret, device.step, device.t0, device.digits, device.drift)
    generator.time = now_epoch
    return generator


def token_for(device: TOTPDevice, *, secret_hex: str | None = None) -> str:
    """Return a current code for the device from its relocated secret."""
    generator = totp_generator(device, secret_hex=secret_hex)
    return f"{generator.token():0{device.digits}d}"


def fresh_token_for(device: TOTPDevice) -> str:
    """Return the next step's code, for a device a sign-in already used.

    The sign-in consumed the current step (``last_t``); the verifier's one-step
    tolerance admits the next one, so a test can prove a second genuine code in
    the same transaction as the session stamp it must bind.
    """
    generator = totp_generator(device)
    generator.time += device.step
    return f"{generator.token():0{device.digits}d}"


def create_totp_device(
    user_id: UUID,
    *,
    confirmed: bool,
) -> TOTPDevice:
    from django_otp.plugins.otp_totp.models import TOTPDevice  # noqa: PLC0415

    # clinic_app can no longer write a device's verification state, so seed the
    # device and its resolver-held secret as clinic_resolver (the migration role
    # is a member), exactly as the enrolment definer would.
    device_secret = secrets.token_bytes(20)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_resolver")
        cursor.execute(
            "INSERT INTO clinic_app.otp_totp_totpdevice "
            "(user_id, name, confirmed, key, step, t0, digits, tolerance, drift, "
            "last_t, throttling_failure_count, created_at, last_used_at) "
            "VALUES (%s, 'Clinic OS authenticator', %s, '', 30, 0, 6, 1, 0, -1, 0, "
            "statement_timestamp(), NULL) RETURNING id",
            [str(user_id), confirmed],
        )
        device_id = int(cursor.fetchone()[0])
        cursor.execute(
            "INSERT INTO clinic_app.identity_totpsecret (device_id, secret) "
            "VALUES (%s, %s)",
            [device_id, device_secret],
        )
    _TEST_TOTP_SECRETS[device_id] = device_secret
    with runtime_role(), current_user_guc(user_id):
        return TOTPDevice.objects.get(pk=device_id, user_id=user_id)


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
