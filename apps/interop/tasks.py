"""Bulk-queue entrypoint for import batches, metered per organization.

The message carries only an opaque batch id. The worker resolves the stored
scope, spends one token of the organization's ``bulk`` fairness bucket
(deferring with a countdown when the tenant is over quota or the meter is
down) and then commits under the requester's re-checked current authority.
"""

from __future__ import annotations

import os
from typing import Final, Protocol
from uuid import UUID

from celery import shared_task
from ops.release.activation import require_live_runtime

from apps.core.fairness import DEFER_COUNTDOWN_SECONDS, fair_acquire
from apps.identity.current_context import CurrentActorError
from apps.interop.access import ImportAccessDeniedError, batch_scope
from apps.interop.commit import run_batch
from apps.tenancy.db import TenantAccessDeniedError, tenant_context

QUEUE: Final = "bulk"
WAITING_COUNTDOWN_SECONDS: Final = 30


class _TaskSender(Protocol):
    """The typed Celery publication surface; only opaque ids enter the broker."""

    def apply_async(self, *, kwargs: dict[str, str], countdown: int = 0) -> None: ...


def commit_batch(*, batch_id: str) -> str:
    """Admit, then commit one stored batch; a replay or late delivery is inert."""
    require_live_runtime(os.environ)
    identifier = UUID(batch_id)
    scope = batch_scope(identifier)
    if scope is None:
        return "missing"
    if not fair_acquire(organization_id=scope.organization_id, queue=QUEUE):
        commit_batch_job.apply_async(
            kwargs={"batch_id": batch_id}, countdown=DEFER_COUNTDOWN_SECONDS
        )
        return "deferred"
    try:
        with tenant_context(scope.actor_id, scope.organization_id):
            outcome = run_batch(clinic_id=scope.clinic_id, batch_id=identifier)
    except (CurrentActorError, TenantAccessDeniedError, ImportAccessDeniedError):
        return "denied"
    if outcome.state == "waiting":
        commit_batch_job.apply_async(
            kwargs={"batch_id": batch_id}, countdown=WAITING_COUNTDOWN_SECONDS
        )
    return outcome.state


commit_batch_job: _TaskSender = shared_task(name="apps.interop.tasks.commit_batch")(
    commit_batch
)
