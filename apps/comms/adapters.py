"""Provider-facing comms adapter contracts for the integration boundary.

No provider is selected or approved yet (docs/integrations). These protocols
pin the boundary shape only: preparation happens inside the tenant
transaction, the network send happens outside it, and callback
authentication runs before any stored operation or tenant is resolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

    from apps.comms.models import IntegrationOperation


class CommsAdapter(Protocol):
    """Define the future comms integration boundary."""

    def send_message(self) -> None:
        """Send a patient message through a Phase >=1 integration."""
        ...


@dataclass(frozen=True, slots=True)
class OperationScope:
    """Stored tenant scope resolved for one operation or callback."""

    operation_id: UUID
    organization_id: UUID
    clinic_id: UUID
    actor_id: UUID


@dataclass(frozen=True, slots=True)
class SendResult:
    """Minimal provider acceptance reference; never body or credential data."""

    provider_reference: str


class TransientSendError(Exception):
    """Report a retryable provider failure; the operation stays claimable."""


class PermanentSendError(Exception):
    """Report a non-retryable provider failure; the operation fails closed."""


class SendAdapter(Protocol):
    """Prepare inside the tenant transaction; send outside it."""

    provider: str

    def prepare(self, operation: IntegrationOperation) -> object:
        """Build the provider request from tenant data inside the transaction."""
        ...

    def send(self, prepared: object, *, operation_id: UUID) -> SendResult:
        """Perform the external call with no open database transaction."""
        ...


@dataclass(frozen=True, slots=True)
class AuthenticatedCallback:
    """Verified callback facts; external tenant claims are never carried."""

    event_id: str
    provider_reference: str
    status: Literal["delivered", "failed"]


class CallbackAuthenticationError(Exception):
    """Reject a callback that fails provider authentication."""

    def __init__(self) -> None:
        """Expose one stable non-identifying rejection message."""
        super().__init__("provider callback authentication failed")


class CallbackAuthenticator(Protocol):
    """Authenticate raw callbacks for one provider before scope resolution."""

    provider: str

    def authenticate(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
    ) -> AuthenticatedCallback:
        """Verify the provider signature and return only verified fields."""
        ...
