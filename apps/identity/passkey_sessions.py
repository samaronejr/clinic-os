"""Rotated, persisted passkey verification and current-policy revalidation."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from django.contrib.auth import SESSION_KEY
from django.db import connection
from django_otp import DEVICE_ID_SESSION_KEY

from apps.identity.models import User
from apps.identity.passkey_challenges import (
    PASSKEY_SESSION_KEY,
    STEP_UP_SESSION_KEY,
    PasskeyDeniedError,
    now_seconds,
)
from apps.identity.session_inventory import (
    coarse_network,
    register_session,
    session_hash,
    supersede_session_hash,
    user_agent_family,
)

if TYPE_CHECKING:
    from django.contrib.sessions.backends.base import SessionBase
    from django.http import HttpRequest


def record_passkey_verification(request: HttpRequest, *, passkey_id: UUID) -> None:
    """Rotate the existing session before binding the exact verified credential."""
    user = request.user
    if not isinstance(user, User) or not user.is_active:
        raise PasskeyDeniedError
    old_key = request.session.session_key
    request.session.pop(DEVICE_ID_SESSION_KEY, None)
    request.session.cycle_key()
    new_key = request.session.session_key
    if new_key is None:
        raise PasskeyDeniedError
    if old_key is not None:
        supersede_session_hash(session_key=old_key)
    register_session(
        user_id=user.pk,
        session_key=new_key,
        expires_at=request.session.get_expiry_date(),
        network=coarse_network(request.META.get("REMOTE_ADDR", "")),
        family=user_agent_family(request.META.get("HTTP_USER_AGENT", "")),
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.identity_session_verify_passkey(%s, %s)",
            [session_hash(new_key), passkey_id],
        )
        if cursor.fetchone() != (True,):
            raise PasskeyDeniedError
    request.session[PASSKEY_SESSION_KEY] = str(passkey_id)
    request.session[STEP_UP_SESSION_KEY] = now_seconds()


def is_passkey_verified(*, session: SessionBase, max_age: int | None = None) -> bool:
    """Recheck own credential, live inventory, membership and current org policy."""
    raw_id = session.get(PASSKEY_SESSION_KEY)
    key = session.session_key
    if not isinstance(raw_id, str) or key is None:
        return False
    verified_at = session.get(STEP_UP_SESSION_KEY)
    if type(verified_at) is not int:
        return False
    try:
        passkey_id = UUID(raw_id)
        signed_user_id = UUID(session.get(SESSION_KEY, ""))
    except (ValueError, TypeError):
        return False
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.identity_passkey_session_verified(%s, %s, %s) "
            "AND NULLIF(current_setting('app.current_user_id', true), '')::uuid = %s",
            [session_hash(key), passkey_id, max_age, signed_user_id],
        )
        result = cursor.fetchone()
        return result is not None and result[0] is True
