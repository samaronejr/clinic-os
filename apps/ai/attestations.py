"""Purpose-separated server attestations using one dedicated managed key."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import rfc8785

from apps.ai.action_contracts import ActionRefusedError, Refusal
from apps.ai.action_payload import JSON, digest_text
from apps.core.secrets import secret_store
from apps.identity.current_context import current_actor_id
from apps.identity.stepup import STEP_UP_SESSION_KEY, assert_step_up

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest

KEY_ID: Final = "agent-attestation-v1"


def attest(*, purpose: str, value: dict[str, JSON]) -> str:
    """Use a fixed key name; payload key ids can never select another secret."""
    key = bytes.fromhex(secret_store().get_secret(KEY_ID))
    frame = purpose.encode("ascii") + b"\x00" + rfc8785.dumps(value)
    return hmac.new(key, frame, hashlib.sha256).hexdigest()


def verify(*, purpose: str, value: dict[str, JSON], signature: str) -> None:
    """Unknown key ids and cross-purpose replay fail identically."""
    if value.get("key_id") != KEY_ID or not hmac.compare_digest(
        attest(purpose=purpose, value=value), signature
    ):
        raise ActionRefusedError(Refusal.ATTESTATION)


@dataclass(frozen=True, slots=True)
class StepUpEvidence:
    """An immutable challenge receipt; its fields alone confer no authority."""

    evidence_id: UUID
    clinic_id: UUID
    user_id: UUID
    digest: str
    verified_at: int
    signature: str

    def value(self) -> dict[str, JSON]:
        """Return the exact material authenticated by the server."""
        return {
            "id": str(self.evidence_id),
            "clinic_id": str(self.clinic_id),
            "user_id": str(self.user_id),
            "digest": self.digest,
            "verified_at": self.verified_at,
            "key_id": KEY_ID,
        }


def step_up_evidence(
    request: HttpRequest,
    *,
    clinic_id: UUID,
    digest: str,
) -> StepUpEvidence:
    """Adapt the actual verified HTTP session, never a posted evidence reference."""
    assert_step_up(request)
    user = current_actor_id()
    if request.user.pk != user:
        raise ActionRefusedError(Refusal.AUTHORITY)
    digest_text(digest)
    verified_at = request.session[STEP_UP_SESSION_KEY]
    evidence = StepUpEvidence(uuid4(), clinic_id, user, digest, verified_at, "")
    return StepUpEvidence(
        evidence.evidence_id,
        clinic_id,
        user,
        digest,
        verified_at,
        attest(purpose="clinic-agent-step-up-v1", value=evidence.value()),
    )
