"""Fresh persisted factor and database-bound enterprise administration."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.utils import timezone

from apps.identity.current_context import CurrentActorError, current_actor_id
from apps.identity.models import Clinic
from apps.identity.passkey_challenges import PasskeyDeniedError
from apps.identity.passkey_controls import require_authenticator_session
from apps.identity.session_inventory import session_hash

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime
    from uuid import UUID


def utc_now() -> datetime:
    """Return the shared, replaceable expiry clock without a timer wait."""
    return timezone.now()


class GovernanceDeniedError(PermissionDenied):
    """Use one payload-free refusal for unknown, foreign and unauthorized scope."""


def require_synthetic_identity() -> None:
    """Only the deterministic fixture transport is shipped; real legs are EG-bound."""
    if (
        getattr(settings, "CLINIC_ENTERPRISE_SYNTHETIC", False) is not True
        or settings.CLINIC_DATA_MODE != "synthetic"
    ):
        raise GovernanceDeniedError


@contextmanager
def factor_scope(*, session_key: str) -> Iterator[UUID]:
    """Bind an actual persisted TOTP/current-policy passkey to the GUC actor."""
    if not connection.in_atomic_block:
        raise GovernanceDeniedError
    try:
        actor = current_actor_id()
        require_authenticator_session(session_key=session_key)
    except (CurrentActorError, PasskeyDeniedError) as error:
        raise GovernanceDeniedError from error
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.enterprise_factor',true)")
        row = cursor.fetchone()
        previous = "" if row is None or row[0] is None else row[0]
        cursor.execute(
            "SELECT set_config('app.enterprise_factor',%s,true)",
            [session_hash(session_key).hex()],
        )
        try:
            yield actor
        finally:
            cursor.execute(
                "SELECT set_config('app.enterprise_factor',%s,true)", [previous]
            )


@contextmanager
def admin_scope(*, clinic_id: UUID, session_key: str) -> Iterator[tuple[UUID, Clinic]]:
    """Require current organization administration in every unit, never one role."""
    with factor_scope(session_key=session_key) as actor, connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.enterprise_org_admin(%s)", [clinic_id])
        if cursor.fetchone() != (True,):
            raise GovernanceDeniedError
        clinic = Clinic.objects.filter(pk=clinic_id).first()
        if clinic is None:
            raise GovernanceDeniedError
        yield actor, clinic
