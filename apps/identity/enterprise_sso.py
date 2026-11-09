"""Single-use, server-session-bound primary authentication with explicit scope."""

from __future__ import annotations

import hmac
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict
from uuid import UUID

from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.db import connection, transaction
from django.db.models.functions import Now

from apps.audit.services import record_phase1_event
from apps.identity.enterprise_authority import (
    GovernanceDeniedError,
    require_synthetic_identity,
)
from apps.identity.enterprise_oidc import (
    SyntheticOidcAdapter,
    now_seconds,
    pkce_challenge,
)
from apps.identity.models import SsoConnection, User
from apps.identity.scim_schema import blind_index
from apps.tenancy.db import clear_connection_tenant_gucs

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.contrib.sessions.backends.base import SessionBase

FLOW_KEY = "enterprise_sso_flow"
MAX_FLOW_AGE_SECONDS = 300
MAX_FLOW_VALUE_LENGTH = 1024


class SsoFlow(TypedDict):
    """Represent the complete shape after the stored flow passes validation."""

    clinic_id: str
    connection_id: str
    issuer: str
    audience: str
    verifier: str
    nonce: str
    state: str
    issued_at: int


@dataclass(frozen=True, slots=True)
class FederatedLogin:
    """Verified stored identity, not caller or IdP-supplied tenant authority."""

    user: User
    organization_id: UUID


@contextmanager
def _connection_scope(*, clinic_id: UUID) -> Iterator[SsoConnection]:
    require_synthetic_identity()
    if connection.in_atomic_block:
        raise GovernanceDeniedError
    try:
        with transaction.atomic(durable=True), connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM clinic_app.enterprise_connection_scope(%s)", [clinic_id]
            )
            row = cursor.fetchone()
            if row is None:
                raise GovernanceDeniedError
            connection_id, organization_id = row
            cursor.execute(
                "SELECT set_config('app.current_tenant',%s,true),"
                "set_config('app.current_sso_connection',%s,true),"
                "set_config('app.current_user_id','',true)",
                [str(organization_id), str(connection_id)],
            )
            target = SsoConnection.objects.filter(pk=connection_id).first()
            if target is None:
                raise GovernanceDeniedError
            yield target
    finally:
        clear_connection_tenant_gucs()


def begin_sso(*, clinic_id: UUID, session: SessionBase) -> dict[str, object]:
    """Start code/PKCE authentication without granting membership or MFA freshness."""
    with _connection_scope(clinic_id=clinic_id) as target:
        configuration = target.configuration
        issuer, audience = configuration["issuer"], configuration["client_id"]
        discovery = SyntheticOidcAdapter.discovery(issuer=issuer)
        verifier, nonce, state = (
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(32),
        )
        session[FLOW_KEY] = {
            "clinic_id": str(clinic_id),
            "connection_id": str(target.pk),
            "issuer": issuer,
            "audience": audience,
            "verifier": verifier,
            "nonce": nonce,
            "state": state,
            "issued_at": now_seconds(),
        }
    session.save()
    return {
        "state": state,
        "nonce": nonce,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
        "response_type": "code",
        "authorization_endpoint": discovery["authorization_endpoint"],
        "synthetic": True,
    }


def _consume_flow(session: SessionBase) -> SsoFlow:
    key = session.session_key
    if key is None:
        raise GovernanceDeniedError
    with transaction.atomic():
        persisted = (
            Session.objects.select_for_update()
            .filter(session_key=key, expire_date__gt=Now())
            .first()
        )
        if persisted is None:
            raise GovernanceDeniedError
        data = persisted.get_decoded()
        flow = data.pop(FLOW_KEY, None)
        if flow is None:
            raise GovernanceDeniedError
        fresh = SessionStore(session_key=key)
        fresh.update(data)
        fresh.save()
        session.pop(FLOW_KEY, None)
    if (
        not isinstance(flow, dict)
        or set(flow)
        != {
            "clinic_id",
            "connection_id",
            "issuer",
            "audience",
            "verifier",
            "nonce",
            "state",
            "issued_at",
        }
        or type(flow["issued_at"]) is not int
        or not 0 <= now_seconds() - flow["issued_at"] <= MAX_FLOW_AGE_SECONDS
        or not all(
            isinstance(value, str) and 0 < len(value) <= MAX_FLOW_VALUE_LENGTH
            for key, value in flow.items()
            if key != "issued_at"
        )
    ):
        raise GovernanceDeniedError
    return SsoFlow(
        clinic_id=flow["clinic_id"],
        connection_id=flow["connection_id"],
        issuer=flow["issuer"],
        audience=flow["audience"],
        verifier=flow["verifier"],
        nonce=flow["nonce"],
        state=flow["state"],
        issued_at=flow["issued_at"],
    )


def finish_sso(*, state: str, code: str, session: SessionBase) -> FederatedLogin:
    """Consume before verifying; only a provisioned stable issuer/sub may sign in."""
    require_synthetic_identity()
    flow = _consume_flow(session)
    if not isinstance(state, str) or not hmac.compare_digest(state, flow["state"]):
        raise GovernanceDeniedError
    try:
        clinic_id = UUID(flow["clinic_id"])
    except (ValueError, TypeError) as error:
        raise GovernanceDeniedError from error
    with _connection_scope(clinic_id=clinic_id) as target:
        if (
            str(target.pk) != flow["connection_id"]
            or target.configuration["issuer"] != flow["issuer"]
            or target.configuration["client_id"] != flow["audience"]
        ):
            raise GovernanceDeniedError
        subject = SyntheticOidcAdapter.exchange(
            code=code,
            issuer=flow["issuer"],
            audience=flow["audience"],
            nonce=flow["nonce"],
            verifier=flow["verifier"],
        )
        digest = blind_index(connection_id=target.pk, field="externalId", value=subject)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.enterprise_sso_principal(%s,%s)",
                [target.pk, digest],
            )
            principal = cursor.fetchone()
            if principal is None or principal[0] is None:
                raise GovernanceDeniedError
            cursor.execute(
                "SELECT set_config('app.current_user_id',%s,true)",
                [str(principal[0])],
            )
            cursor.execute(
                "SELECT * FROM clinic_app.enterprise_sso_account(%s,%s)",
                [target.pk, digest],
            )
            row = cursor.fetchone()
            if row is None:
                raise GovernanceDeniedError
            user_id, username, encoded_password, resource_id = row
            cursor.execute(
                "SELECT set_config('app.current_user_id',%s,true)", [str(user_id)]
            )
            record_phase1_event(
                "identity.sso.authenticated",
                clinic_id=clinic_id,
                affected_record_id=resource_id,
            )
        return FederatedLogin(
            User(
                id=user_id, username=username, password=encoded_password, is_active=True
            ),
            target.organization_id,
        )
