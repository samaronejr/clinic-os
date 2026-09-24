from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from apps.identity.models import User, UserClinicRole
from django.contrib.auth.hashers import make_password
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection, transaction
from django.http import HttpRequest, HttpResponse
from django.test import Client
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.middleware import OTPMiddleware

from otp_test_support import create_totp_device, runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from uuid import UUID

    from django_otp.plugins.otp_totp.models import TOTPDevice

    from rbac_fixtures import RbacGraph

STEP_UP_NOW: Final = 2_100_000_000
STEP_UP_MAX_AGE: Final = 300
STEP_UP_SESSION_KEY: Final = "otp_verified_at"


def logged_in_client(user_id: UUID) -> Client:
    user = User.objects.get(pk=user_id)
    client = Client()
    with runtime_role():
        assert client.login(
            username=user.username,
            password=RBAC_RAW_CREDENTIAL,
        )
    return client


def seed_freshness(
    client: Client,
    device: TOTPDevice,
    *,
    verified_at: int = STEP_UP_NOW,
) -> None:
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session[STEP_UP_SESSION_KEY] = verified_at
    session.save()


def clear_freshness(client: Client) -> None:
    session = client.session
    session.pop(STEP_UP_SESSION_KEY, None)
    session.save()


def verified_request(user_id: UUID, *, verified_at: int | None = None) -> HttpRequest:
    """Build the exact request shape OTPMiddleware yields after real step-up.

    The session carries the persistent id of a freshly created confirmed TOTP
    device plus the verification timestamp; the lazy user resolves the device
    from the session exactly as production middleware does, so service-boundary
    checks see a genuine recent-verification authority rather than a flag.
    """
    device = create_totp_device(user_id, confirmed=True)
    request = HttpRequest()
    request.session = SessionStore()
    request.session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    request.session[STEP_UP_SESSION_KEY] = (
        int(time.time()) if verified_at is None else verified_at
    )
    request.user = User.objects.get(pk=user_id)
    OTPMiddleware(lambda _request: HttpResponse())(request)
    return request


def create_role_actor(
    graph: RbacGraph,
    role: UserClinicRole.Role,
) -> User:
    user = User.objects.create(
        username=f"todo10-{role}-{uuid4().hex}",
        password=make_password(RBAC_RAW_CREDENTIAL),
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
            role=role,
        )
    return user
