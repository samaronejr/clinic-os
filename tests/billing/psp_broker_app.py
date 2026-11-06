"""Required-gate worker fixture: a durable fake remote effect precedes SIGKILL."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import TYPE_CHECKING

from apps.billing.psp_contracts import PaymentStatus
from apps.billing.psp_fixtures import RecordedPaymentProvider
from celery.signals import worker_process_init
from config.celery import app

if TYPE_CHECKING:
    from apps.billing.psp_contracts import PaymentRequest

__all__ = ("app",)


class BrokerRecordedProvider(RecordedPaymentProvider):
    """Keep the fake PSP's committed fact outside the killed worker process."""

    def __init__(self, root: Path) -> None:
        super().__init__(secret=b"synthetic-broker-fixture", records={})
        self.root = root

    def lookup(self, *, request: PaymentRequest) -> PaymentStatus | None:
        path = self.root / f"{request.idempotency_key.hex}.json"
        if not path.exists():
            return None
        fields = json.loads(path.read_text())
        return PaymentStatus(
            fields["provider_reference"], "settled", fields["amount_minor"], "BRL", 2
        )

    def create_charge(self, *, request: PaymentRequest) -> PaymentStatus:
        with (self.root / "remote-create-attempts").open("a") as attempts:
            attempts.write(f"{request.idempotency_key.hex}\n")
            attempts.flush()
            os.fsync(attempts.fileno())
        path = self.root / f"{request.idempotency_key.hex}.json"
        with path.open("x") as effect:
            json.dump(
                {
                    "provider_reference": f"fixture-{request.idempotency_key.hex}",
                    "amount_minor": request.amount_minor,
                },
                effect,
            )
            effect.flush()
            os.fsync(effect.fileno())
        os.kill(os.getpid(), signal.SIGKILL)
        raise AssertionError


def install_recorded_provider(**kwargs: object) -> None:
    """Install only in the explicitly required broker gate's worker children."""
    from apps.billing.psp_worker import register_payment_provider  # noqa: PLC0415

    del kwargs
    if os.environ.get("CLINIC_BROKER_GATE") != "required":
        return
    register_payment_provider(
        BrokerRecordedProvider(Path(os.environ["CLINIC_PSP_GATE_ROOT"]))
    )


worker_process_init.connect(install_recorded_provider, weak=False)
