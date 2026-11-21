"""Test-only provider fakes with state that survives a killed Celery child."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING
from unittest.mock import patch

from apps.comms.channel_adapters import SMSReminderAdapter
from apps.core.integration import register_send_adapter
from apps.prescription.signature_provider import (
    SYNTHETIC_PROVIDER,
    SyntheticSignatureProvider,
    signature_provider_for,
)
from celery import current_task
from django.db import connection
from redis import Redis

from resilience.death_worker import record_effect

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    from apps.comms.adapters import SendResult
    from apps.prescription.signature_provider import (
        CallbackSignatureProvider,
        SignatureAcceptance,
        SignatureRequest,
    )


def _hold_after_acceptance() -> None:
    """Tell the parent exactly which prefork child to kill, then wait there."""
    assert not connection.in_atomic_block
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("clinic_app",)
    with Redis.from_url(os.environ["CELERY_BROKER_URL"]) as redis:
        redis.rpush("accepted", json.dumps([os.getpid(), current_task.request.id]))
        redis.blpop("never-release", timeout=90)
    msg = "gate must SIGKILL the accepting worker before it resumes"
    raise AssertionError(msg)


class _MessagingProvider(SMSReminderAdapter):
    def __init__(self, ledger: Path) -> None:
        self.ledger = ledger

    def send(self, prepared: object, *, operation_id: UUID) -> SendResult:
        result = super().send(prepared, operation_id=operation_id)
        first = not self.ledger.exists()
        record_effect(self.ledger, result.provider_reference)
        if first:
            _hold_after_acceptance()
        return result


class _SigningProvider(SyntheticSignatureProvider):
    def __init__(self, ledger: Path) -> None:
        self.ledger = ledger

    def begin(self, request: SignatureRequest) -> SignatureAcceptance:
        acceptance = super().begin(request)
        reference = acceptance.operation_ref
        record_effect(self.ledger.with_suffix(".begins"), reference)
        prior = self.ledger.read_text().splitlines() if self.ledger.exists() else []
        if reference not in prior:
            record_effect(self.ledger, reference)
            if not prior:
                _hold_after_acceptance()
        return acceptance


def install(ledger: Path) -> None:
    """Leave production prepare, claim, provider-key derivation and bind intact."""
    register_send_adapter(_MessagingProvider(ledger))

    def provider_for(name: str) -> CallbackSignatureProvider:
        provider = signature_provider_for(name)
        return _SigningProvider(ledger) if name == SYNTHETIC_PROVIDER else provider

    patch("apps.prescription.signing.signature_provider_for", provider_for).start()
