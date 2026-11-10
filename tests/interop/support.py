"""Shared interop fixtures: platform secrets, org admins, partner clients, tokens."""

from __future__ import annotations

import base64
import math
import secrets
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from apps.identity.models import User, UserClinicRole
from apps.interop import ratelimit
from apps.interop.management import IssuedCredential, create_client
from apps.interop.ratelimit import Decision
from apps.interop.webhook_transport import SYNTHETIC_TRANSPORT
from apps.tenancy.db import tenant_context
from django.contrib.auth.hashers import make_password
from django.test import Client

from auth.stepup_test_support import create_role_actor
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph

ALL_SCOPES = (
    "appointments:read",
    "availability:read",
    "invoices.status:read",
    "patients.demographics:read",
    "webhooks:manage",
)
PLATFORM_SECRETS = ("interop-external-ids", "interop-webhook-signing")


@pytest.fixture
def interop_secrets(synthetic_secret_backend: Path) -> Path:
    """Provision the external-id and webhook-signing platform secrets."""
    for name in PLATFORM_SECRETS:
        path = synthetic_secret_backend / f"{name}.secret"
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return synthetic_secret_backend


@dataclass
class LocalBucket:
    """The production token-bucket arithmetic in process, under a frozen clock.

    Gated tests prove the same arithmetic in the Redis Lua script; this keeps
    every other API test independent of a broker while metering stays real.
    """

    now: float = 1_700_000_000.0
    buckets: dict[str, tuple[float, float]] = field(default_factory=dict)
    unavailable: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def spend(self, *, client_id: UUID, per_minute: int, purpose: str) -> Decision:
        if self.unavailable:
            raise ratelimit.RateLimitUnavailableError
        key = f"{purpose}:{client_id}"
        rate = per_minute / 60.0
        with self._lock:
            tokens, touched = self.buckets.get(key, (float(per_minute), self.now))
            tokens = min(per_minute, tokens + max(0.0, self.now - touched) * rate)
            if tokens >= 1:
                self.buckets[key] = (tokens - 1, self.now)
                return Decision(allowed=True, retry_after_seconds=0)
            self.buckets[key] = (tokens, self.now)
            wait = math.ceil((1 - tokens) / rate * 1000)
            return Decision(
                allowed=False, retry_after_seconds=max(1, math.ceil(wait / 1000))
            )


@pytest.fixture
def local_bucket(monkeypatch: pytest.MonkeyPatch) -> LocalBucket:
    """Install the in-process bucket for API tests that do not prove Redis."""
    bucket = LocalBucket()
    monkeypatch.setattr(ratelimit, "_backend", bucket)
    return bucket


@pytest.fixture
def webhook_transport() -> Iterator[None]:
    """Start and finish every test with an empty synthetic transport."""
    SYNTHETIC_TRANSPORT.reset()
    yield
    SYNTHETIC_TRANSPORT.reset()


def org_admin(graph: RbacGraph, role: str = "org_admin") -> UUID:
    """Create an administrator holding ``role`` on every clinic of organization A."""
    user = create_role_actor(graph, UserClinicRole.Role(role))
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            user=user,
            role=role,
        )
    return user.pk


def org_b_admin(graph: RbacGraph) -> UUID:
    """Create an administrator of organization B (its only clinic C)."""
    user = User.objects.create(
        username=f"sintetico-org-b-admin-{uuid4().hex}",
        password=make_password(RBAC_RAW_CREDENTIAL),
    )
    with owner_context(graph.organization_b):
        UserClinicRole.objects.create(
            organization_id=graph.organization_b,
            clinic_id=graph.clinic_c,
            user=user,
            role="org_admin",
        )
    return user.pk


def make_client(  # noqa: PLR0913 - one partner registration's terms
    graph: RbacGraph,
    admin: UUID,
    scopes: Sequence[str] = ALL_SCOPES,
    *,
    organization: UUID | None = None,
    clinic: UUID | None = None,
    rate: int = 600,
) -> IssuedCredential:
    """Register a partner client through the real management service."""
    with runtime_role(), tenant_context(admin, organization or graph.organization_a):
        return create_client(
            clinic_id=clinic or graph.clinic_a,
            name="Parceiro Sintetico",
            scopes=list(scopes),
            rate_limit_per_minute=rate,
            command_id=uuid4(),
        )


def basic(credential: IssuedCredential) -> str:
    raw = f"{credential.client_id}:{credential.client_secret}".encode("ascii")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def request_token(
    http: Client, credential: IssuedCredential, scope: str | None = None
) -> _MonkeyPatchedWSGIResponse:
    data = {"grant_type": "client_credentials"}
    if scope is not None:
        data["scope"] = scope
    return token_post(http, data, authorization=basic(credential))


def token_post(
    http: Client, data: dict[str, str], *, authorization: str | None = None
) -> _MonkeyPatchedWSGIResponse:
    """POST one RFC 6749 form-encoded token request."""
    headers = {} if authorization is None else {"Authorization": authorization}
    with runtime_role():
        return http.post(
            "/api/v1/oauth/token",
            urlencode(data),
            content_type="application/x-www-form-urlencoded",
            headers=headers,
        )


def access_token(credential: IssuedCredential, scope: str | None = None) -> str:
    response = request_token(Client(), credential, scope)
    assert response.status_code == 200, response.content
    token = response.json()["access_token"]
    assert isinstance(token, str)
    return token


def api_get(http: Client, token: str, path: str) -> _MonkeyPatchedWSGIResponse:
    with runtime_role():
        return http.get(path, HTTP_AUTHORIZATION=f"Bearer {token}")


def api_post(
    http: Client,
    token: str,
    path: str,
    body: object,
    *,
    key: str | None = None,
) -> _MonkeyPatchedWSGIResponse:
    headers = {"Authorization": f"Bearer {token}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    with runtime_role():
        return http.post(path, body, content_type="application/json", headers=headers)
