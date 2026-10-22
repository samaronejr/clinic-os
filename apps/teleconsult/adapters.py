"""Outbox send adapter for the synthetic video provider.

The provider-neutral boundary is ``apps.teleconsult.video_providers``
(``VideoProvider``: create room, mint token, revoke). This module binds the
synthetic provider to the committed outbox: preparation reads stored room or
credential rows inside the tenant transaction and the provider call happens
outside it. A synthetic room or revoke reference is never a live provider
receipt. Live adapters are not registered: their legs are BLOCKED-ON-EG.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from django.conf import settings

from apps.comms.adapters import PermanentSendError, SendResult, TransientSendError
from apps.teleconsult.capabilities import room_capability
from apps.teleconsult.models import TeleconsultCredential, TeleconsultRoom
from apps.teleconsult.video_providers import (
    SYNTHETIC_PROVIDER,
    ProviderRequestError,
    RevokeSpec,
    RoomSpec,
    SyntheticVideoProvider,
)

if TYPE_CHECKING:
    from apps.comms.models import IntegrationOperation

PROVIDER: str = SYNTHETIC_PROVIDER
ROOM_SUBJECT: str = "teleconsult.session"
PARTICIPANT_SUBJECT: str = "teleconsult.participant"


@dataclass(frozen=True, slots=True)
class PreparedRoom:
    """Ephemeral minimal room request; never stored or logged."""

    session_id: UUID
    room_name: str


@dataclass(frozen=True, slots=True)
class PreparedRevoke:
    """Ephemeral participant disconnect request; role identity only."""

    room_name: str
    identity: str


class SyntheticRoomAdapter:
    """Create synthetic rooms and revoke participants through the outbox only."""

    provider: str = PROVIDER

    def prepare(self, operation: IntegrationOperation) -> PreparedRoom | PreparedRevoke:
        """Read the stored room or revoked credential inside the tenant txn."""
        if operation.subject_type == PARTICIPANT_SUBJECT:
            credential = (
                TeleconsultCredential.objects.filter(
                    pk=operation.subject_id, revoked_at__isnull=False
                )
                .select_related("session__room")
                .first()
            )
            if credential is None:
                raise PermanentSendError
            return PreparedRevoke(
                room_name=credential.session.room.room_name,
                identity=credential.role,
            )
        room = TeleconsultRoom.objects.filter(operation=operation).first()
        if room is None:
            raise PermanentSendError
        return PreparedRoom(session_id=room.session_id, room_name=room.room_name)

    def send(self, prepared: object, *, operation_id: UUID) -> SendResult:
        """Return only an explicitly synthetic reference, or fail closed."""
        if (
            not isinstance(prepared, (PreparedRoom, PreparedRevoke))
            or not isinstance(operation_id, UUID)
            or not room_capability().synthetic_enabled
        ):
            raise PermanentSendError
        # Explicit fault injection only on the already-enabled synthetic path.
        if getattr(settings, "TELECONSULT_SYNTHETIC_FAIL", False):
            raise TransientSendError
        provider = SyntheticVideoProvider()
        if isinstance(prepared, PreparedRevoke):
            try:
                provider.revoke(RevokeSpec(prepared.room_name, prepared.identity))
            except ProviderRequestError as error:
                raise PermanentSendError from error
            return SendResult(f"synthetic:revoke:{operation_id}")
        try:
            return SendResult(
                provider.create_room(RoomSpec(prepared.room_name)).reference
            )
        except ProviderRequestError:
            # Rooms stored before opaque names keep their v1 reference shape.
            return SendResult(f"synthetic:room:{prepared.room_name}")
