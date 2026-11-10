"""Fair ``bulk``-queue entrypoints: import batches and organization exports.

Messages carry only opaque references. An import batch resolves its stored
scope, spends one token of the organization's ``bulk`` fairness bucket
(deferring with a countdown when the tenant is over quota or the meter is
down) and then commits under the requester's re-checked current authority.
Organization exports are admitted the same way before they build.
"""

from __future__ import annotations

import os
from typing import Final, Protocol
from uuid import UUID

from celery import shared_task
from django.db import connection
from ops.release.activation import require_live_runtime

from apps.core.fairness import DEFER_COUNTDOWN_SECONDS, acquire_or_defer, fair_acquire
from apps.identity.current_context import CurrentActorError
from apps.interop.access import ImportAccessDeniedError, batch_scope
from apps.interop.commit import run_batch
from apps.interop.export_builder import build_organization_export
from apps.tenancy.db import TenantAccessDeniedError, tenant_context

QUEUE: Final = "bulk"
WAITING_COUNTDOWN_SECONDS: Final = 30


class _BatchSender(Protocol):
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


commit_batch_job: _BatchSender = shared_task(name="apps.interop.tasks.commit_batch")(
    commit_batch
)


class _TaskSender(Protocol):
    """Publish only opaque references through the registered Celery task."""

    def apply_async(self, *, kwargs: dict[str, str], queue: str) -> object: ...


class _Request(Protocol):
    """The bound request the fair-queue deferral re-publishes."""

    args: tuple[object, ...]
    kwargs: dict[str, object]


class _BoundTask(Protocol):
    """A bound Celery task as ``acquire_or_defer`` uses it."""

    @property
    def request(self) -> _Request: ...

    def apply_async(self, **options: object) -> object: ...


def dispatch_export(
    *, export_id: UUID, organization_id: UUID, requested_by_id: UUID
) -> None:
    """Hand one committed export request to the bulk worker."""
    require_live_runtime(os.environ)
    build_export.apply_async(
        kwargs={
            "export_id": str(export_id),
            "organization_id": str(organization_id),
            "requested_by_id": str(requested_by_id),
        },
        queue="bulk",
    )


def run_build(
    task: _BoundTask, *, export_id: str, organization_id: str, requested_by_id: str
) -> str:
    """Build after this organization's fair-queue admission; defer otherwise."""
    organization = UUID(organization_id)
    if not acquire_or_defer(task, organization_id=organization, queue="bulk"):
        return "deferred"
    return build_organization_export(
        export_id=UUID(export_id),
        organization_id=organization,
        requested_by_id=UUID(requested_by_id),
    )


def dispatch_due() -> int:
    """Re-dispatch requested exports whose after-commit handoff never arrived."""
    require_live_runtime(os.environ)
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.interop_due_exports()")
        due = cursor.fetchall()
    for export_id, organization_id, requested_by_id in due:
        dispatch_export(
            export_id=export_id,
            organization_id=organization_id,
            requested_by_id=requested_by_id,
        )
    return len(due)


def expire() -> int:
    """Expire past-due archives and fail exports whose requester lost authority."""
    require_live_runtime(os.environ)
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.interop_expire_exports()")
        row = cursor.fetchone()
    return 0 if row is None else int(row[0])


build_export: _TaskSender = shared_task(
    name="apps.interop.tasks.build_export",
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
)(run_build)
dispatch_due_exports = shared_task(name="apps.interop.tasks.dispatch_due_exports")(
    dispatch_due
)
expire_exports = shared_task(name="apps.interop.tasks.expire_exports")(expire)
