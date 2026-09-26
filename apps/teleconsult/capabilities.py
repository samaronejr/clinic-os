"""Fail-closed synthetic room decision gated on the lifecycle registry.

There is no selected or owner-authorized video provider in record set
2026-09-24-v2 (docs/integrations/records/2026-09-24-v2/video.md).
``real_enabled`` is answered by ``apps.providers.services.is_live``: the
``video`` capability must be ``activated`` in the registry AND the process
must hold the live data mode with ``require_live_runtime`` satisfied.
Configuration cannot turn a synthetic room reference into live approval.
"""

from dataclasses import dataclass

from django.conf import settings

from apps.providers.services import is_live


@dataclass(frozen=True, slots=True)
class RoomCapability:
    """A synthetic decision plus the registry-gated live answer."""

    synthetic_enabled: bool
    real_enabled: bool
    reason: str = "missing_video_provider_and_owner_approval"


def room_capability() -> RoomCapability:
    """Require explicit opt-in for the synthetic room provider."""
    enabled = bool(getattr(settings, "TELECONSULT_SYNTHETIC_PROVIDER", False))
    return RoomCapability(
        synthetic_enabled=enabled,
        real_enabled=is_live("video", clinic_id=None),
    )
