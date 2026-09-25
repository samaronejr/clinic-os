"""Short, freshly authenticated subscription checks; no idle DB connection."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Final, cast
from uuid import UUID

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import SESSION_KEY, get_user
from django.db import connection
from django.http import HttpRequest
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.models import Device
from django_otp.plugins.otp_totp.models import TOTPDevice
from ops.release.activation import require_live_runtime

from apps.audit.services import record_phase1_event
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import User
from apps.identity.otp import is_privileged_user
from apps.intake.patient_access import PATIENT_SESSION_KEY, patient_session_context
from apps.tenancy.db import TenantAccessDeniedError, tenant_context

if TYPE_CHECKING:
    from django.contrib.sessions.backends.base import SessionBase

    from apps.identity.otp import TotpDevice

MAX_TOPICS: Final = 8
MAX_TOPIC_LENGTH: Final = 100
CLINIC_TOPIC: Final = re.compile(
    r"clinic:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):"
    r"(agenda|inbox|queue|messages)"
)
# V1 has no clinic-wide clinical-inbox or messaging permission. Do not infer
# either from demographic access. Their owning todos must add an explicit
# permission and this mapping together. read_own cannot grant clinic timing.
TOPIC_PERMISSIONS: Final = {"agenda": "appointment.read", "queue": "appointment.read"}


class TopicDeniedError(Exception):
    """Use the same denial for malformed, absent, expired and forbidden scope."""

    def __init__(self) -> None:
        """Never reflect a topic, session key or database error."""
        super().__init__("subscription unavailable")


@dataclass(frozen=True, slots=True)
class Subscription:
    """Server-side authority reference, never sent in an event."""

    session_key: str
    user_id: UUID
    topics: tuple[str, ...]


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
        if match is None or match[2] not in TOPIC_PERMISSIONS:
            # ai_job:<opaque> is reserved until apps/ai owns durable job scope.
            # In particular, knowledge of an opaque id is NOT authorization.
            raise TopicDeniedError
        clinic_id = UUID(match[1])
        require_permission(TOPIC_PERMISSIONS[match[2]], clinic_id=clinic_id)
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


def authorize_topics_sync(session_key: str, topics: tuple[str, ...]) -> Subscription:
    """Reload session, password hash, OTP, membership and permissions, then close.

    A session id is a reference, not a cached authentication result. The check
    uses the normal auth backend inside exactly one short tenant transaction.
    Patient sessions never acquire staff GUCs or clinic-wide activity rights.
    No patient/job topic exists yet; valid and invalid sessions both fail closed
    until an owning domain supplies a patient-scoped contract.
    """
    try:
        require_live_runtime(os.environ)
        topics = validated_topics(topics)
        session: SessionBase = import_module(settings.SESSION_ENGINE).SessionStore(
            session_key=session_key
        )
        if patient_id := session.get(PATIENT_SESSION_KEY):
            with patient_session_context(UUID(patient_id)):
                raise TopicDeniedError
        user_id = _verified_staff(session, topics)
        return Subscription(session_key, user_id, topics)
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


async def authorize_topics(session_key: str, topics: tuple[str, ...]) -> Subscription:
    """Perform one bounded synchronous check off the ASGI event loop."""
    return await sync_to_async(authorize_topics_sync, thread_sensitive=True)(
        session_key, topics
    )
