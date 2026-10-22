"""Own credential removal and explicit fresh, organization-wide policy controls."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import connection, transaction
from django.db.models.functions import Now

from apps.audit.services import record_phase1_event
from apps.identity.current_context import (
    CurrentActorError,
    current_actor_id,
    require_current_actor_clinic_roles,
)
from apps.identity.models import Clinic, Passkey, UserClinicRole
from apps.identity.passkey_challenges import PasskeyDeniedError
from apps.identity.session_inventory import session_hash

if TYPE_CHECKING:
    from uuid import UUID


def require_authenticator_session(*, session_key: str) -> None:
    """Require a persisted fresh actual factor, including current passkey policy."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.identity_session_authenticator_verified(%s)",
            [session_hash(session_key)],
        )
        if cursor.fetchone() != (True,):
            raise PasskeyDeniedError


def require_registration_session(*, session_key: str) -> None:
    """Permit fresh primary enrollment without replacing default privileged TOTP."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.identity_passkey_registration_allowed(%s)",
            [session_hash(session_key)],
        )
        if cursor.fetchone() != (True,):
            raise PasskeyDeniedError


def own_passkeys() -> tuple[Passkey, ...]:
    """Read only the current user's current-organization credential history."""
    try:
        user_id = current_actor_id()
    except CurrentActorError as error:
        raise PasskeyDeniedError from error
    return tuple(Passkey.objects.filter(user_id=user_id).order_by("-created_at", "id"))


def remove_passkey(*, clinic_id: UUID, passkey_id: UUID, session_key: str) -> None:
    """Remove an own credential under fresh enrollment authority."""
    try:
        user_id = require_current_actor_clinic_roles(
            clinic_id=clinic_id, roles=tuple(UserClinicRole.Role)
        )
    except CurrentActorError as error:
        raise PasskeyDeniedError from error
    with transaction.atomic():
        passkey = (
            Passkey.objects.select_for_update()
            .filter(pk=passkey_id, user_id=user_id)
            .first()
        )
        if passkey is None:
            raise PasskeyDeniedError
        require_registration_session(session_key=session_key)
        if passkey.removed_at is None:
            Passkey.objects.filter(pk=passkey.pk).update(removed_at=Now())
            record_phase1_event(
                "identity.passkey.removed",
                clinic_id=clinic_id,
                affected_record_id=passkey.pk,
            )


def set_passkey_policy(*, clinic_id: UUID, enabled: bool, session_key: str) -> None:
    """Require current staff.organization in every unit, plus an actual fresh factor."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.identity_passkey_policy_set(%s, %s, %s)",
            [clinic_id, enabled, session_hash(session_key)],
        )
        result = cursor.fetchone()
        if result not in {(0,), (1,)}:
            raise PasskeyDeniedError
        if result == (1,):
            clinic = Clinic.objects.get(pk=clinic_id)
            record_phase1_event(
                "identity.passkey.policy_changed",
                clinic_id=clinic_id,
                affected_record_id=clinic.organization_id,
            )
