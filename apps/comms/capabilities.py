"""Independent fail-closed channel decisions for task-6's unavailable records.

There is no selected or owner-authorized real provider in record set
2026-09-12-v1. Configuration cannot turn a synthetic receipt into live approval.
A future approved provider must implement its own reviewed adapter and gate.
"""

from dataclasses import dataclass
from typing import Final

from django.conf import settings

CHANNELS: Final = ("email", "sms", "whatsapp")


@dataclass(frozen=True, slots=True)
class ChannelCapability:
    """A synthetic-only decision; real use remains explicitly blocked."""

    channel: str
    synthetic_enabled: bool
    real_enabled: bool = False
    reason: str = "missing_provider_credentials_and_owner_approval"


def channel_capability(channel: str) -> ChannelCapability:
    """Require explicit opt-in independently for each synthetic channel."""
    if channel not in CHANNELS:
        message = "unknown reminder channel"
        raise ValueError(message)
    enabled = channel in getattr(settings, "COMMS_SYNTHETIC_CHANNELS", ())
    return ChannelCapability(channel=channel, synthetic_enabled=enabled)


def template_enabled(channel: str, version: int) -> bool:
    """Revocation is per channel and exact immutable template version."""
    revoked = getattr(settings, "COMMS_REVOKED_REMINDER_TEMPLATES", ())
    return version == 1 and f"{channel}:{version}" not in revoked
