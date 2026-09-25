"""Short, freshly authenticated subscription checks; no idle DB connection."""

from __future__ import annotations

import os
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Final, cast
from uuid import UUID

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import SESSION_KEY, get_user
from django.contrib.sessions.models import Session
from django.db import connection
from django.http import HttpRequest
from django.utils import timezone
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.models import Device
from django_otp.plugins.otp_totp.models import TOTPDevice
from ops.release.activation import require_live_runtime

from apps.audit.services import record_phase1_event
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import User
from apps.identity.otp import is_privileged_user
from apps.intake.patient_access import PATIENT_SESSION_KEY, patient_session_context
from apps.realtime.scopes import authorize_scope
from apps.realtime.topics import (
    CLINIC_TOPIC,
    JOB_TOPIC,
    PATIENT_TOPIC,
    TOPIC_PERMISSIONS,
)
from apps.tenancy.db import TenantAccessDeniedError, tenant_context

if TYPE_CHECKING:
    from datetime import datetime

    from django.contrib.sessions.backends.base import SessionBase

    from apps.identity.otp import TotpDevice

MAX_TOPICS: Final = 8
MAX_TOPIC_LENGTH: Final = 100


class TopicDeniedError(Exception):
    """Use the same denial for malformed, absent, expired and forbidden scope."""

    def __init__(self) -> None:
        """Never reflect a topic, session key or database error."""
        super().__init__("subscription unavailable")


class TopicExpiredError(TopicDeniedError):
    """Same HTTP denial, with an explicit terminal stream state."""


@dataclass(frozen=True, slots=True)
class Subscription:
    """Server-side authority reference, never sent in an event."""

    session_key: str
    user_id: UUID | None
    topics: tuple[str, ...]
    expires_at: datetime | None = None
    patient_session_id: UUID | None = None


def validated_topics(value: object) -> tuple[str, ...]:
    """Bound untrusted subscription input before any database or Redis work."""
    if (
        not isinstance(value, (list, tuple))
        or not 1 <= len(value) <= MAX_TOPICS
        or any(
            not isinstance(item, str) or len(item) > MAX_TOPIC_LENGTH for item in value
        )
    ):
        raise TopicDeniedError
    topics = tuple(str(item) for item in value)
    if len(set(topics)) != len(topics):
        raise TopicDeniedError
    return topics


def _staff_topics(topics: tuple[str, ...]) -> None:
    for topic in topics:
        match = CLINIC_TOPIC.fullmatch(topic)
        if match is not None:
            clinic_id = UUID(match[1])
            if match[2] in {"inbox", "messages"}:
                authorize_scope(topic=topic)
            else:
                require_permission(TOPIC_PERMISSIONS[match[2]], clinic_id=clinic_id)
        elif JOB_TOPIC.fullmatch(topic):
            clinic_id = authorize_scope(topic=topic)
        else:
            raise TopicDeniedError
        record_phase1_event(
            "realtime.subscription.authorized",
            clinic_id=clinic_id,
            affected_record_id=clinic_id,
        )


def _verified_staff(session: SessionBase, topics: tuple[str, ...]) -> UUID:
    user_id = UUID(session[SESSION_KEY])
    org_id = UUID(session["active_org_id"])
    with tenant_context(user_id, org_id):
        request = HttpRequest()
        request.session = session
        user = get_user(request)
        if not isinstance(user, User) or not user.is_active:
            raise TopicDeniedError
        if is_privileged_user(user):
            persistent_id = session.get(DEVICE_ID_SESSION_KEY)
            device = (
                Device.from_persistent_id(persistent_id)
                if isinstance(persistent_id, str)
                else None
            )
            if not isinstance(device, TOTPDevice):
                raise TopicDeniedError
            confirmed = cast("TotpDevice", device)
            if not confirmed.confirmed or confirmed.user_id != user_id:
                raise TopicDeniedError
        _staff_topics(topics)
        return user_id


def _patient_topics(
    session_key: str,
    patient_id: UUID,
    topics: tuple[str, ...],
    expires_at: datetime,
) -> Subscription:
    with patient_session_context(patient_id) as binding:
        if binding is None:
            raise TopicDeniedError
        for topic in topics:
            match = PATIENT_TOPIC.fullmatch(topic)
            if (
                match is None
                or UUID(match[1]) != binding.enrollment_id
                or match[2] not in binding.operations
            ):
                raise TopicDeniedError
        return Subscription(
            session_key,
            None,
            topics,
            min(expires_at, binding.expires_at, binding.idle_expires_at),
            patient_id,
        )


def authorize_topics_sync(*, session_key: str, topics: tuple[str, ...]) -> Subscription:
    """Reload session, password hash, OTP, membership and permissions, then close.

    A session id is a reference, not a cached authentication result. The check
    uses the normal auth backend inside exactly one short tenant transaction.
    Patient sessions never acquire staff GUCs or clinic-wide activity rights.
    Patient topics bind the exact enrollment and allowed patient operation.
    """
    try:
        require_live_runtime(os.environ)
        topics = validated_topics(topics)
        expires_at = (
            Session.objects.filter(session_key=session_key)
            .values_list("expire_date", flat=True)
            .first()
        )
        if expires_at is None:
            raise TopicDeniedError
        if expires_at <= timezone.now():
            raise TopicExpiredError
        session: SessionBase = import_module(settings.SESSION_ENGINE).SessionStore(
            session_key=session_key
        )
        if patient_id := session.get(PATIENT_SESSION_KEY):
            return _patient_topics(session_key, UUID(patient_id), topics, expires_at)
        user_id = _verified_staff(session, topics)
        return Subscription(session_key, user_id, topics, expires_at)
    except (
        KeyError,
        TypeError,
        ValueError,
        CurrentActorError,
        TenantAccessDeniedError,
    ) as error:
        raise TopicDeniedError from error
    finally:
        # CONN_MAX_AGE=0 alone only closes at request end, not between yields.
        # This executes on the SAME sync worker that did all session/ORM work.
        connection.close()


async def authorize_topics(
    *, session_key: str, topics: tuple[str, ...]
) -> Subscription:
    """Perform one bounded synchronous check off the ASGI event loop."""
    return await sync_to_async(authorize_topics_sync, thread_sensitive=True)(
        session_key=session_key, topics=topics
    )
