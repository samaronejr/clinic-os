"""Domain-owned checks on direct reads of retained scribe material."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.ai.contracts import GatewayRefusedError, Purpose

if TYPE_CHECKING:
    from collections.abc import Callable

    from apps.ai.models import AIArtifact

_READERS: dict[Purpose, Callable[[AIArtifact], bool]] = {}


def register_scribe_reader(
    purpose: Purpose, reader: Callable[[AIArtifact], bool]
) -> None:
    """Install the scribe app's point-of-use decisions during startup."""
    if purpose not in (Purpose.SCRIBE_ASR, Purpose.SCRIBE_DRAFT):
        msg = "not a scribe purpose"
        raise ValueError(msg)
    _READERS[purpose] = reader


def check_retained_input(source: AIArtifact) -> None:
    """Deny raw inputs and unapplied output without a registered scribe reader."""
    purpose = Purpose(source.purpose)
    if purpose in (Purpose.SCRIBE_ASR, Purpose.SCRIBE_DRAFT):
        reader = _READERS.get(purpose)
        if reader is None or not reader(source):
            raise GatewayRefusedError(reason="authority_revoked")
