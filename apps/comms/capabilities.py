"""Independent fail-closed channel decisions gated on the lifecycle registry.

There is no selected or owner-authorized real provider in record set
2026-09-24-v2. ``real_enabled`` is answered by
``apps.providers.services.is_live``: the channel capability must be
``activated`` in the registry AND the process must hold the live data mode
with ``require_live_runtime`` satisfied. Configuration cannot turn a
synthetic receipt into live approval.
"""

from dataclasses import dataclass
from typing import Final

from django.conf import settings

from apps.providers.services import is_live

CHANNELS: Final = ("email", "sms", "whatsapp")


@dataclass(frozen=True, slots=True)
class ChannelCapability:
    """A synthetic decision plus the registry-gated live answer."""

    channel: str
    synthetic_enabled: bool
    real_enabled: bool
    reason: str = "missing_provider_credentials_and_owner_approval"


def channel_capability(channel: str) -> ChannelCapability:
    """Require explicit opt-in independently for each synthetic channel."""
    if channel not in CHANNELS:
        message = "unknown reminder channel"
        raise ValueError(message)
    enabled = channel in getattr(settings, "COMMS_SYNTHETIC_CHANNELS", ())
    return ChannelCapability(
        channel=channel,
        synthetic_enabled=enabled,
        real_enabled=is_live(channel, clinic_id=None),
    )


def template_enabled(channel: str, version: int) -> bool:
    """Revocation is per channel and exact immutable template version."""
    revoked = getattr(settings, "COMMS_REVOKED_REMINDER_TEMPLATES", ())
    return version == 1 and f"{channel}:{version}" not in revoked
