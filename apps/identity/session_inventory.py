"""User-owned hash-only session records and authenticated lifecycle hooks."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from http import HTTPStatus
from ipaddress import ip_network
from typing import TYPE_CHECKING
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.db.models.functions import Now

from apps.audit.services import record_phase1_event
from apps.identity.current_context import CurrentActorError, current_actor_id
from apps.identity.models import SessionRecord, User, UserClinicRole

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

    from django.http import HttpRequest


class SessionInventoryDeniedError(PermissionDenied):
    """Reject an unknown, foreign, stale or unbound session selector."""


@contextmanager
def _authenticated_user(user_id: UUID) -> Iterator[None]:
    """Bind only identity supplied by a successful framework auth/OTP hook."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.current_user_id', true)")
        row = cursor.fetchone()
        previous = "" if row is None or row[0] is None else str(row[0])
        if previous and previous != str(user_id):
            raise SessionInventoryDeniedError
        cursor.execute(
            "SELECT set_config('app.current_user_id', %s, true)", [str(user_id)]
        )
        try:
            with transaction.atomic():
                yield
        finally:
            cursor.execute(
                "SELECT set_config('app.current_user_id', %s, true)", [previous]
            )


def session_hash(session_key: str) -> bytes:
    """Digest a framework-owned session key; never retain its plaintext."""
    return hashlib.sha256(session_key.encode("ascii")).digest()


def coarse_network(address: str) -> str:
    """Keep only a /24 network, not the address supplied by the transport."""
    try:
        return str(ip_network(f"{address}/24", strict=False))
    except ValueError:
        return ""


def user_agent_family(header: str) -> str:
    """Reduce the raw header to one of four fixed browser families."""
    if "Firefox/" in header:
        return SessionRecord.UserAgentFamily.FIREFOX
    if any(value in header for value in ("Chrome/", "Chromium/", "Edg/")):
        return SessionRecord.UserAgentFamily.CHROMIUM
    if "Safari/" in header:
        return SessionRecord.UserAgentFamily.SAFARI
    return SessionRecord.UserAgentFamily.OTHER


def register_session(
    *,
    user_id: UUID,
    session_key: str,
    expires_at: datetime,
    network: str,
    family: str,
) -> UUID:
    """Record the authenticated login signal's identity and opaque key digest."""
    with _authenticated_user(user_id), connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.identity_session_register(%s, %s, %s, %s, %s)",
            [user_id, session_hash(session_key), network, family, expires_at],
        )
        row = cursor.fetchone()
    if row is None or row[0] is None:
        raise SessionInventoryDeniedError
    return UUID(str(row[0]))


def register_login_session(
    sender: type[User],
    *,
    request: HttpRequest,
    user: User,
    **kwargs: object,
) -> None:
    """Capture a real Django login without writing any user row."""
    del sender, kwargs
    if not user.is_active:
        return
    key = request.session.session_key
    if key is None:
        # Switching authenticated accounts flushes the key before this signal.
        request.session.save()
        key = request.session.session_key
    if key is None:
        raise SessionInventoryDeniedError
    register_session(
        user_id=user.pk,
        session_key=key,
        expires_at=request.session.get_expiry_date(),
        network=coarse_network(request.META.get("REMOTE_ADDR", "")),
        family=user_agent_family(request.META.get("HTTP_USER_AGENT", "")),
    )


def session_is_active(*, user_id: UUID, session_key: str) -> bool:
    """Reject expired or revoked signed sessions before a tenant is opened."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.identity_session_active(%s, %s)",
            [user_id, session_hash(session_key)],
        )
        row = cursor.fetchone()
        return row is not None and row[0] is True


def session_cookie_is_revoked(*, session_key: str) -> bool:
    """Read only an explicit revocation tombstone when its payload is gone."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.identity_session_revoked_cookie(%s)",
            [session_hash(session_key)],
        )
        row = cursor.fetchone()
        return row is not None and row[0] is True


def supersede_session_hash(*, session_key: str) -> None:
    """Record a framework rotation separately from an explicit revocation."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.identity_session_rotate_hash(%s)",
            [session_hash(session_key)],
        )


def revoke_session_hash(*, session_key: str) -> bool:
    """Revoke the opaque key held by an authenticated logout or rotation."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.identity_session_revoke_hash(%s)",
            [session_hash(session_key)],
        )
        row = cursor.fetchone()
        return row is not None and row[0] is True


def revoke_logout_session(
    sender: type[User],
    *,
    request: HttpRequest,
    user: User | None,
    **kwargs: object,
) -> None:
    """Preserve a logout tombstone before Django removes its server session."""
    del sender, kwargs
    key = request.session.session_key
    if key is not None and user is not None:
        with _authenticated_user(user.pk):
            revoke_session_hash(session_key=key)


def record_verified_session(
    *,
    request: HttpRequest,
    device_id: int,
    old_key: str | None,
) -> None:
    """Bind fresh actual TOTP verification to the rotated session digest."""
    new_key = request.session.session_key
    user = request.user
    if new_key is None or not isinstance(user, User) or not user.is_active:
        raise SessionInventoryDeniedError
    with _authenticated_user(user.pk), connection.cursor() as cursor:
        if old_key is not None:
            supersede_session_hash(session_key=old_key)
        register_session(
            user_id=user.pk,
            session_key=new_key,
            expires_at=request.session.get_expiry_date(),
            network=coarse_network(request.META.get("REMOTE_ADDR", "")),
            family=user_agent_family(request.META.get("HTTP_USER_AGENT", "")),
        )
        cursor.execute(
            "SELECT clinic_app.identity_session_verify(%s, %s)",
            [session_hash(new_key), device_id],
        )
        if cursor.fetchone() != (True,):
            raise SessionInventoryDeniedError


def require_verified_session(*, session_key: str) -> None:
    """Require this user's current persisted fresh actual TOTP verification."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.identity_session_verified(%s)",
            [session_hash(session_key)],
        )
        if cursor.fetchone() != (True,):
            raise SessionInventoryDeniedError


def require_session_step_up(*, clinic_id: UUID, session_key: str) -> None:
    """Require the current exact-clinic manager's persisted fresh TOTP binding."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.identity_session_step_up(%s, %s)",
            [session_hash(session_key), clinic_id],
        )
        if cursor.fetchone() != (True,):
            raise SessionInventoryDeniedError


def own_sessions() -> tuple[SessionRecord, ...]:
    """List only this authenticated user's minimized session metadata."""
    try:
        user_id = current_actor_id()
    except CurrentActorError as error:
        raise SessionInventoryDeniedError from error
    return tuple(
        SessionRecord.objects.filter(user_id=user_id).order_by("-created_at", "id")
    )


def revoke_session(*, session_record_id: UUID) -> None:
    """Revoke one own row; unknown and foreign records have the same refusal."""
    try:
        user_id = current_actor_id()
    except CurrentActorError as error:
        raise SessionInventoryDeniedError from error
    with transaction.atomic():
        record = (
            SessionRecord.objects.select_for_update()
            .filter(pk=session_record_id, user_id=user_id)
            .first()
        )
        clinic_id = (
            UserClinicRole.objects.filter(user_id=user_id)
            .order_by("clinic_id")
            .values_list("clinic_id", flat=True)
            .first()
        )
        if record is None or clinic_id is None:
            raise SessionInventoryDeniedError
        if record.revoked_at is None:
            SessionRecord.objects.filter(pk=record.pk).update(revoked_at=Now())
            record_phase1_event(
                "identity.session.revoked",
                clinic_id=clinic_id,
                affected_record_id=record.pk,
            )


def touch_session(*, session_key: str | None, http_status: int) -> None:
    """Update successful request metadata, never a refusal's session state."""
    if session_key is None or http_status >= HTTPStatus.BAD_REQUEST:
        return
    SessionRecord.objects.filter(
        session_key_hash=session_hash(session_key),
        user_id=current_actor_id(),
        revoked_at__isnull=True,
        expires_at__gt=Now(),
    ).update(last_seen=Now())
