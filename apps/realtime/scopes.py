"""Signed, bounded transport scopes; current DB permissions are never cached.

Clinic inbox/message scopes are explicit operator-issued opt-ins, not inferred
from demographic access. Job scopes bind the creating actor and originating
permission. These transport leases grant no domain read/write authority.
"""

from __future__ import annotations

import logging
import secrets
from functools import partial
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.conf import settings
from django.core import signing
from django.db import transaction
from redis.exceptions import RedisError

from apps.identity.current_context import (
    CurrentActorError,
    current_actor_id,
    require_permission,
)
from apps.identity.models import UserClinicRole
from apps.identity.permissions import PERMISSIONS
from apps.realtime.transport import publish, redis_client, topic_hash

if TYPE_CHECKING:
    from collections.abc import Mapping

SCOPE_TTL: Final = 3600
MAX_SCOPE_BYTES: Final = 2048
SCOPE_SALT: Final = "realtime-scope-v1"
logger = logging.getLogger(__name__)
_DENIED = "subscription unavailable"


def _key(topic: str, user_id: UUID | None) -> str:
    return "rt-scope:" + topic_hash(topic) + (":" + str(user_id) if user_id else "")


def _store(key: str, payload: str) -> None:
    try:
        with redis_client() as client:
            client.set(key, payload, ex=SCOPE_TTL)
    except RedisError:
        logger.warning("realtime scope unavailable; polling required")


def _schedule_scope(
    topic: str,
    user_id: UUID,
    clinic_id: UUID,
    permission: str,
    enrollment_id: UUID | None,
) -> None:
    payload = signing.dumps(
        {
            "topic": topic,
            "user": str(user_id),
            "clinic": str(clinic_id),
            "permission": permission,
            "enrollment": str(enrollment_id) if enrollment_id else None,
        },
        salt=SCOPE_SALT,
    )
    if settings.REALTIME_ENABLED:
        transaction.on_commit(
            partial(
                _store,
                _key(topic, user_id if topic.startswith("clinic:") else None),
                payload,
            )
        )


def grant_clinic_topic(
    *,
    clinic_id: UUID,
    user_id: UUID,
    kind: str,
    permission: str,
) -> str:
    """Opt a member into one clinic topic for an hour; requires staff administration.

    The owning domain explicitly supplies its action permission. Recipients must
    still pass that live permission; the lease never supplies content access.
    """
    require_permission("staff.organization", clinic_id=clinic_id)
    if (
        kind not in {"inbox", "messages"}
        or permission not in PERMISSIONS
        or not UserClinicRole.objects.filter(
            clinic_id=clinic_id, user_id=user_id
        ).exists()
    ):
        raise CurrentActorError(_DENIED)
    topic = f"clinic:{clinic_id}:{kind}"
    _schedule_scope(topic, user_id, clinic_id, permission, None)
    return topic


def _revoke(topic: str, user_id: UUID) -> None:
    with redis_client() as client:
        client.delete(_key(topic, user_id))
    publish(topic=f"authz:user:{user_id}", kind="revoked", version=1)


def revoke_clinic_topic(*, clinic_id: UUID, user_id: UUID, kind: str) -> None:
    """Remove a topic lease before publishing the commit-bound recheck trigger."""
    require_permission("staff.organization", clinic_id=clinic_id)
    if kind not in {"inbox", "messages"}:
        raise CurrentActorError(_DENIED)
    transaction.on_commit(partial(_revoke, f"clinic:{clinic_id}:{kind}", user_id))


def register_job_topic(
    *,
    clinic_id: UUID,
    permission: str,
    patient_enrollment_id: UUID | None = None,
) -> str:
    """Bind an opaque job to its actual actor and originating domain permission.

    The producer calls this in its job-creation transaction, then publishes only
    after commit. Patient-scoped clinical permissions retain their enrollment,
    registration and care-team checks on every subscription authorization.
    """
    user_id = require_permission(
        permission, clinic_id=clinic_id, patient_enrollment_id=patient_enrollment_id
    )
    topic = "ai_job:" + secrets.token_urlsafe(24)
    _schedule_scope(topic, user_id, clinic_id, permission, patient_enrollment_id)
    return topic


def authorize_scope(*, topic: str) -> UUID:
    """Verify the server's binding and then rerun the original DB authority."""
    if not settings.REALTIME_ENABLED:
        raise CurrentActorError(_DENIED)
    user_id = current_actor_id()
    with redis_client() as client:
        raw = client.get(_key(topic, user_id if topic.startswith("clinic:") else None))
    if not isinstance(raw, bytes) or len(raw) > MAX_SCOPE_BYTES:
        raise CurrentActorError(_DENIED)
    try:
        value: Mapping[str, object] = signing.loads(
            raw.decode("ascii"), salt=SCOPE_SALT, max_age=SCOPE_TTL
        )
        if not isinstance(value, dict) or set(value) != {
            "topic",
            "user",
            "clinic",
            "permission",
            "enrollment",
        }:
            raise CurrentActorError(_DENIED)
        clinic_id = UUID(str(value["clinic"]))
        enrollment = (
            UUID(str(value["enrollment"])) if value["enrollment"] is not None else None
        )
        permission = value["permission"]
        if (
            value["topic"] != topic
            or value["user"] != str(user_id)
            or not isinstance(permission, str)
            or permission not in PERMISSIONS
        ):
            raise CurrentActorError(_DENIED)
        require_permission(
            permission, clinic_id=clinic_id, patient_enrollment_id=enrollment
        )
    except (ValueError, TypeError, signing.BadSignature) as error:
        raise CurrentActorError(_DENIED) from error
    return clinic_id
