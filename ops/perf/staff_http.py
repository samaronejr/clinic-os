"""Real staff sign-in (password + TOTP) and timed page requests."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING

from ops.perf.dataset import PASSWORD, Tenant

if TYPE_CHECKING:
    from django.test import Client


@dataclass(frozen=True, slots=True)
class StaffLogin:
    """One synthetic receptionist with a confirmed authenticator device."""

    username: str
    device: str
    secret: bytes


class StaffLoginError(RuntimeError):
    """Raise when the sign-in flow does not reach an authenticated session."""


def provision_logins(tenant: Tenant, count: int) -> list[StaffLogin]:
    """Create ``count`` receptionists with TOTP devices as the owner would."""
    from apps.identity.models import User, UserClinicRole  # noqa: PLC0415
    from django.contrib.auth.hashers import make_password  # noqa: PLC0415
    from django.db import connection, transaction  # noqa: PLC0415

    password_hash = make_password(PASSWORD)
    logins: list[StaffLogin] = []
    for _ in range(count):
        user = User.objects.create(
            username=f"perf-session-{secrets.token_hex(8)}", password=password_hash
        )
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                [str(tenant.organization_id)],
            )
            UserClinicRole.objects.create(
                user_id=user.pk,
                organization_id=tenant.organization_id,
                clinic_id=tenant.clinic_id,
                role=UserClinicRole.Role.RECEPTIONIST,
            )
        secret = secrets.token_bytes(20)
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE clinic_resolver")
            cursor.execute(
                "INSERT INTO clinic_app.otp_totp_totpdevice "
                "(user_id, name, confirmed, key, step, t0, digits, tolerance, "
                "drift, last_t, throttling_failure_count, created_at, "
                "last_used_at) VALUES (%s, 'Perf authenticator', true, '', 30, "
                "0, 6, 1, 0, -1, 0, statement_timestamp(), NULL) RETURNING id",
                [str(user.pk)],
            )
            row = cursor.fetchone()
            if row is None:
                raise StaffLoginError
            device_id = int(row[0])
            cursor.execute(
                "INSERT INTO clinic_app.identity_totpsecret (device_id, secret) "
                "VALUES (%s, %s)",
                [device_id, secret],
            )
        logins.append(
            StaffLogin(user.username, f"otp_totp.totpdevice/{device_id}", secret)
        )
    return logins


def sign_in(client: Client, login: StaffLogin) -> None:
    """Run the password step then the TOTP step; both must redirect."""
    from django_otp.oath import TOTP  # noqa: PLC0415

    first = client.post(
        "/auth/login/", {"username": login.username, "password": PASSWORD}
    )
    generator = TOTP(login.secret, 30, 0, 6, 0)
    generator.time = time.time()
    token = f"{generator.token():06d}"
    second = client.post(
        "/auth/verify/", {"otp_device": login.device, "otp_token": token}
    )
    redirect = HTTPStatus.FOUND
    if first.status_code != redirect or second.status_code != redirect:
        message = "synthetic staff sign-in did not complete"
        raise StaffLoginError(message)


def timed_get(client: Client, path: str) -> tuple[float, int]:
    """Return the elapsed milliseconds and status of one authenticated GET."""
    started = time.perf_counter()
    response = client.get(path, follow=True)
    return (time.perf_counter() - started) * 1000.0, response.status_code


def http_worker(
    logins: list[StaffLogin], paths: list[str], count: int
) -> tuple[list[float], int]:
    """Sign in every login in this process, then time ``count`` page requests.

    Runs in a spawned process (its own GIL), so the server-side Python work of
    concurrent sessions does not serialize behind the load generator.
    """
    from django.test import Client  # noqa: PLC0415

    from ops.perf.runtime import CLIENT_HOST, runtime_role  # noqa: PLC0415

    samples: list[float] = []
    errors = 0
    with runtime_role():
        clients = []
        for login in logins:
            client = Client(HTTP_HOST=CLIENT_HOST)
            sign_in(client, login)
            clients.append(client)
        for step in range(count):
            elapsed, status = timed_get(
                clients[step % len(clients)], paths[step % len(paths)]
            )
            samples.append(elapsed)
            if status != HTTPStatus.OK:
                errors += 1
    return samples, errors
