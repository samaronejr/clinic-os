from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import uuid4

from apps.identity.models import User, UserClinicRole
from apps.identity.session_inventory import record_verified_session
from apps.identity.stepup import stamp_step_up_verification
from django.contrib.auth.hashers import make_password
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection, transaction
from django.http import HttpRequest, HttpResponse
from django.test import Client
from django.utils import timezone
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.middleware import OTPMiddleware
from django_otp.plugins.otp_totp.models import TOTPDevice

from otp_test_support import create_totp_device, current_user_guc, runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from uuid import UUID

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


def verified_request(
    user_id: UUID, *, verified_at: int | None = None, persisted: bool = False
) -> HttpRequest:
    """Build the exact request shape OTPMiddleware yields after real step-up.

    The session carries the persistent id of a freshly created confirmed TOTP
    device plus the verification timestamp; the lazy user resolves the device
    from the session exactly as production middleware does, so service-boundary
    checks see a genuine recent-verification authority rather than a flag.
    Default freshness uses the verifier's production clock; explicit timestamps
    remain available for stale and future-verification cases.

    ``persisted`` also saves the session and records the verification the way
    the production TOTP boundary does, for SQL that rechecks the session itself
    (agent approval votes). A later membership change revokes that session, so
    build it after the actor's roles are final.
    """
    device = create_totp_device(user_id, confirmed=True)
    request = HttpRequest()
    request.session = SessionStore()
    request.session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    if verified_at is None:
        stamp_step_up_verification(request)
    else:
        request.session[STEP_UP_SESSION_KEY] = verified_at
    request.user = User.objects.get(pk=user_id)
    if persisted:
        # Before OTPMiddleware: its lazy user must resolve the device later,
        # under the caller's actor context, not inside this owner bookkeeping.
        _persist_verification(request, device)
    OTPMiddleware(lambda _request: HttpResponse())(request)
    return request


def _persist_verification(request: HttpRequest, device: TOTPDevice) -> None:
    """Record the device use a real token check writes, then bind the session."""
    request.session.save()
    now = timezone.now()
    with runtime_role(), current_user_guc(device.user_id):
        TOTPDevice.objects.filter(pk=device.pk).update(
            last_t=int(now.timestamp()) // device.step, last_used_at=now
        )
    with runtime_role():
        record_verified_session(request=request, device_id=device.pk, old_key=None)


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
