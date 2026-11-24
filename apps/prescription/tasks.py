"""Broker entrypoint for the prescription signing outbox."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from celery import shared_task

from apps.prescription.signing import (
    DispatchResult,
    ReconciliationResult,
    dispatch_signature,
    reconcile_signature,
)

if TYPE_CHECKING:
    from celery.result import EagerResult


class _SignatureTask(Protocol):
    def apply_async(self, *, kwargs: dict[str, str]) -> object: ...

    def apply(self, *, kwargs: dict[str, str], throw: bool) -> EagerResult: ...


def _execute_signature(*, operation_id: str) -> DispatchResult | ReconciliationResult:
    """Dispatch prepared work or verify the stored candidate, never sign again."""
    key = UUID(operation_id)
    dispatch = dispatch_signature(key)
    if dispatch == "reconcile":
        verification = reconcile_signature(key)
        if verification != "skipped":
            return verification
    return dispatch


execute_signature: _SignatureTask = shared_task(name="prescription.dispatch_signature")(
    _execute_signature
)
