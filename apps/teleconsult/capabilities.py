"""Fail-closed synthetic room decision for task-6's unavailable video record.

There is no selected or owner-authorized video provider in record set
2026-09-12-v1 (docs/integrations/records/2026-09-12-v1/video.md). Configuration
cannot turn a synthetic room reference into live approval; a future approved
provider must implement its own reviewed adapter and gate.
"""

from dataclasses import dataclass

from django.conf import settings


@dataclass(frozen=True, slots=True)
class RoomCapability:
    """A synthetic-only decision; real provider use remains explicitly blocked."""

    synthetic_enabled: bool
    real_enabled: bool = False
    reason: str = "missing_video_provider_and_owner_approval"


def room_capability() -> RoomCapability:
    """Require explicit opt-in for the synthetic room provider."""
    enabled = bool(getattr(settings, "TELECONSULT_SYNTHETIC_PROVIDER", False))
    return RoomCapability(synthetic_enabled=enabled)
