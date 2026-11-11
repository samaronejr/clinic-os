"""Scenario 14 worker: perform one provider effect, then die before recording it.

The parent test starts ``python -c "import django; django.setup(); from
resilience.death_worker import main; main()" <family> <operation-id> <ledger>``
against its own test database as ``clinic_app``. The worker appends the remote
effect it caused to the ledger, fsyncs it and SIGKILLs itself, so no ``finally``
or commit runs: exactly the state a worker leaves when it dies after the
provider answered and before the local transition committed.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch
from uuid import UUID

from apps.billing.psp_fixtures import RecordedPaymentProvider
from apps.billing.psp_worker import register_payment_provider
from apps.comms.channel_adapters import EmailReminderAdapter
from apps.comms.models import IntegrationOperation
from apps.core.integration import execute_operation, register_send_adapter
from apps.prescription.signature_provider import SyntheticSignatureProvider
from apps.prescription.signing import dispatch_signature
from apps.tenancy.db import tenant_context
from django.db import connection

if TYPE_CHECKING:
    from datetime import datetime

    from apps.billing.psp_contracts import PaymentRequest, PaymentStatus
    from apps.comms.adapters import SendResult
    from apps.prescription.signature_provider import (
        SignatureAcceptance,
        SignatureRequest,
    )


def record_effect(ledger: Path, line: str) -> None:
    """Make one remote effect durable before the worker dies."""
    descriptor = os.open(ledger, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, f"{line}\n".encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _die() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


class DyingEmailAdapter(EmailReminderAdapter):
    """Accept the reminder synthetically, record it, then die."""

    def __init__(self, ledger: Path) -> None:
        self.ledger = ledger

    def send(self, prepared: object, *, operation_id: UUID) -> SendResult:
        result = super().send(prepared, operation_id=operation_id)
        record_effect(self.ledger, f"messaging:{result.provider_reference}")
        _die()
        return result


class DyingSigner(SyntheticSignatureProvider):
    """Open the provider-side signing operation, record it, then die."""

    def __init__(self, ledger: Path) -> None:
        self.ledger = ledger

    def begin(self, request: SignatureRequest) -> SignatureAcceptance:
        acceptance = super().begin(request)
        record_effect(
            self.ledger, f"signing:{request.operation_id}:{acceptance.operation_ref}"
        )
        _die()
        return acceptance


class DyingPaymentProvider(RecordedPaymentProvider):
    """Create the recorded charge, record it, then die."""

    def __init__(self, ledger: Path) -> None:
        super().__init__(secret=b"synthetic-fixture", records={})
        self.ledger = ledger

    def create_charge(self, *, request: PaymentRequest) -> PaymentStatus:
        status = super().create_charge(request=request)
        record_effect(
            self.ledger,
            f"payment:{request.idempotency_key}:{status.provider_reference}",
        )
        _die()
        return status


def _not_before(operation_id: UUID) -> datetime | None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.comms_operation_scope(%s)", [operation_id]
        )
        row = cursor.fetchone()
    assert row is not None
    organization_id, _, actor_id = row
    with tenant_context(actor_id, organization_id):
        return IntegrationOperation.objects.get(pk=operation_id).not_before


def main() -> None:
    """Run one worker step that dies right after its remote effect."""
    family, operation_id, ledger = sys.argv[1], UUID(sys.argv[2]), Path(sys.argv[3])
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("clinic_app",)
    if family == "messaging":
        register_send_adapter(DyingEmailAdapter(ledger))
        due = _not_before(operation_id)
        with patch("apps.core.integration.timezone", SimpleNamespace(now=lambda: due)):
            execute_operation(operation_id)
    elif family == "signing":
        with patch(
            "apps.prescription.signing.signature_provider_for",
            lambda _name: DyingSigner(ledger),
        ):
            dispatch_signature(operation_id)
    elif family == "payment":
        register_payment_provider(DyingPaymentProvider(ledger))
        execute_operation(operation_id)
    # Reaching this line means the effect never happened.
    raise SystemExit(3)
