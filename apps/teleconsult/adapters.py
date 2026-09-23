"""Provider-facing teleconsult adapter contracts for the outbox boundary.

No video provider is selected or approved yet (docs/integrations). The
synthetic adapter below exercises the real committed-outbox boundary only:
preparation reads stored session/room rows inside the tenant transaction and
the external call happens outside it. A synthetic room reference is never a
live provider receipt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from django.conf import settings

from apps.comms.adapters import PermanentSendError, SendResult, TransientSendError
from apps.teleconsult.capabilities import room_capability
from apps.teleconsult.models import TeleconsultRoom

if TYPE_CHECKING:
    from apps.comms.models import IntegrationOperation

PROVIDER: str = "teleconsult-synthetic-v1"


class TeleconsultAdapter(Protocol):
    """Define the future teleconsult integration boundary."""

    def start_teleconsultation(self) -> None:
        """Start a teleconsultation through a Phase >=1 integration."""
        ...


@dataclass(frozen=True, slots=True)
class PreparedRoom:
    """Ephemeral minimal room request; never stored or logged."""

    session_id: UUID
    room_name: str


class SyntheticRoomAdapter:
    """Create synthetic rooms through the shared send boundary only."""

    provider: str = PROVIDER

    def prepare(self, operation: IntegrationOperation) -> PreparedRoom:
        """Read the stored room row inside the tenant transaction."""
        room = TeleconsultRoom.objects.filter(operation=operation).first()
        if room is None:
            raise PermanentSendError
        return PreparedRoom(session_id=room.session_id, room_name=room.room_name)

    def send(self, prepared: object, *, operation_id: UUID) -> SendResult:
        """Return only an explicitly synthetic room reference, or fail closed."""
        if (
            not isinstance(prepared, PreparedRoom)
            or not isinstance(operation_id, UUID)
            or not room_capability().synthetic_enabled
        ):
            raise PermanentSendError
        # Explicit fault injection only on the already-enabled synthetic path.
        if getattr(settings, "TELECONSULT_SYNTHETIC_FAIL", False):
            raise TransientSendError
        return SendResult(f"synthetic:room:{prepared.room_name}")
