"""Broker entrypoint for the prescription signing outbox."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from celery import shared_task

from apps.prescription.signing import DispatchResult, dispatch_signature

if TYPE_CHECKING:
    from celery.result import EagerResult


class _SignatureTask(Protocol):
    def apply_async(self, *, kwargs: dict[str, str]) -> object: ...

    def apply(self, *, kwargs: dict[str, str], throw: bool) -> EagerResult: ...


def _execute_signature(*, operation_id: str) -> DispatchResult:
    """Resolve and dispatch a prepared signature by its local operation UUID."""
    return dispatch_signature(UUID(operation_id))


execute_signature: _SignatureTask = shared_task(name="prescription.dispatch_signature")(
    _execute_signature
)
